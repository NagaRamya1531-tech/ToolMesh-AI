"""Atomic decisions tested with isolated PostgreSQL and no adapter dispatch."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from os import urandom
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.exceptions import HTTPException
from test_durable_execution_schema import IMAGE, migrate, snapshot
from test_durable_execution_schema import sandbox as sandbox
from test_execution_store import REQUEST, ledger

from gateway.approvals import Approvals
from gateway.execution_approval import ExecutionApprovals
from gateway.execution_contracts import (
    ApprovalState,
    ExecutionState,
    ProtectedIntentScope,
    SealedPayload,
)
from gateway.execution_crypto import (
    PayloadContext,
    PayloadProtection,
    SandboxWrappingKeys,
)
from gateway.execution_store import CommandStoreError
from gateway.identity import PRINCIPALS
from gateway.telemetry import Telemetry
from shared.database import execute, rows

APPROVER = "refund-approver"
REASON = "synthetic-private-decision-reason"
WINDOW = timedelta(minutes=3)
DB = pytest.mark.skipif(not IMAGE, reason="requires disposable PostgreSQL image")


@pytest.fixture(scope="module")
def upgraded(sandbox):
    url, env, _ = sandbox
    before = asyncio.run(snapshot(url))
    migrate(env, "upgrade", "0011")
    assert asyncio.run(snapshot(url)) == before
    yield sandbox
    migrate(env, "downgrade", "0010")
    assert asyncio.run(snapshot(url)) == before


@asynccontextmanager
async def approvals_fixture(upgraded):
    async with ledger(upgraded) as (db, store, payloads, digests):
        service = ExecutionApprovals(db, payloads, digests, WINDOW)
        try:
            yield db, store, service, payloads, digests
        finally:
            async with db.begin() as conn:
                await execute(conn, "DELETE FROM execution_approval_decisions")


async def get_state(db, ident):
    async with db.connect() as conn:
        [item] = await rows(
            conn,
            """
            SELECT a.status AS approval, a.decided_by, a.decided_at, a.decision_reason,
                   c.state AS phase, c.dispatch_not_after, e.status AS execution,
                   e.finished_at,
                   (SELECT revision FROM execution_authorization_guard WHERE singleton) AS revision,
                   (SELECT count(*) FROM execution_approval_decisions WHERE execution_id=:id) AS evidence,
                   (SELECT array_agg(action ORDER BY id) FROM audit_events WHERE resource_id=:id) AS audit,
                   (SELECT array_agg(status ORDER BY id) FROM execution_events WHERE execution_id=:id) AS events,
                   (SELECT count(*) FROM execution_attempts WHERE execution_id=:id) AS attempts
            FROM approvals a JOIN executions e ON e.id=a.id
            JOIN execution_commands c ON c.execution_id=a.id WHERE a.id=:id
            """,
            id=ident,
        )
        return item


def test_validates_dispatch_window_and_reason_without_accessing_database():
    db = create_async_engine("postgresql+asyncpg://", hide_parameters=True)
    payloads = PayloadProtection(SandboxWrappingKeys({1: urandom(32)}, 1))
    from gateway.execution_crypto import CommandDigests

    digests = CommandDigests(urandom(32), urandom(32))
    with pytest.raises(ValueError, match="dispatch window must be positive"):
        ExecutionApprovals(db, payloads, digests, timedelta(0))
    service = ExecutionApprovals(db, payloads, digests, WINDOW)

    async def check():
        for reason in (" ", "x" * 241, "\ud800"):
            with pytest.raises(CommandStoreError, match="^INVALID_DECISION$"):
                await service.decide(uuid4(), APPROVER, "APPROVED", reason)
        await db.dispose()

    asyncio.run(check())


@DB
@pytest.mark.parametrize(
    "decision,phase,projection",
    [
        ("APPROVED", ExecutionState.READY, "RUNNING"),
        ("REJECTED", ExecutionState.REJECTED, "REJECTED"),
    ],
)
def test_decision_commit_and_identical_retry(
    upgraded, decision, phase, projection, caplog
):
    async def check():
        async with approvals_fixture(upgraded) as (db, store, service, payloads, _):
            ident = (await store.submit(REQUEST)).execution_id
            first = await service.decide(ident, APPROVER, decision, REASON)
            second = await service.decide(ident, APPROVER, decision, REASON)
            assert (
                first.approval_status.value == decision
                and first.execution_state is phase
            )
            assert second == replace(first, replayed=True)
            state = await get_state(db, ident)
            assert state["approval"] == decision
            assert state["execution"] == projection and state["events"] == [
                "PENDING_APPROVAL",
                projection,
            ]
            assert state["audit"] == [
                "DURABLE_APPROVAL_REQUESTED",
                f"DURABLE_APPROVAL_{decision}",
            ]
            assert state["evidence"] == 1 and state["attempts"] == 0
            assert state["decided_by"] == APPROVER and state["decision_reason"] is None
            if decision == "APPROVED":
                assert state["dispatch_not_after"] - state["decided_at"] == WINDOW
                assert state["finished_at"] is None
            else:
                assert (
                    state["dispatch_not_after"] is None
                    and state["finished_at"] == state["decided_at"]
                )
            async with db.connect() as conn:
                [sealed] = await rows(
                    conn,
                    "SELECT * FROM execution_approval_decisions WHERE execution_id=:id",
                    id=ident,
                )
            assert sealed["authorization_revision"] == state["revision"]
            context = PayloadContext(
                "approval-decision",
                ident,
                ProtectedIntentScope(
                    workspace_id="default",
                    requester="finance-agent",
                    customer_id="C-102",
                    tool_id="payments.issue_refund",
                ),
            )
            envelope = SealedPayload.model_validate(
                {k: sealed[k] for k in SealedPayload.model_fields}
            )
            assert json.loads(payloads.open(envelope, context)) == REASON
            assert REASON not in str(state) and REASON not in caplog.text
            for conflicting in (
                ("REJECTED" if decision == "APPROVED" else "APPROVED", REASON),
                (decision, "different synthetic reason"),
            ):
                with pytest.raises(
                    CommandStoreError, match="^APPROVAL_ALREADY_DECIDED$"
                ):
                    await service.decide(ident, APPROVER, *conflicting)
            assert await get_state(db, ident) == state

    asyncio.run(check())


@DB
def test_entitlements_are_human_scoped_and_separate_from_requester(
    upgraded, monkeypatch
):
    async def check():
        async with approvals_fixture(upgraded) as (db, store, service, _, _):
            ident = (await store.submit(REQUEST)).execution_id
            # Give an agent an entitlement and a human the wrong tool scope.
            async with db.begin() as conn:
                await execute(
                    conn,
                    """
                    INSERT INTO execution_approval_entitlements(principal,customer_id,tool_id)
                    VALUES ('finance-agent','C-102','payments.issue_refund'),
                           ('platform-admin','C-102','it.restart_service')
                """,
                )
            try:
                for actor in ("finance-agent", "platform-admin", "unknown"):
                    with pytest.raises(
                        CommandStoreError, match="^APPROVAL_NOT_AVAILABLE$"
                    ):
                        await service.decide(ident, actor, "APPROVED", REASON)
                with monkeypatch.context() as patch:
                    # Synthetic identity transition proves separation for a human
                    # whose identity also matches the stored requester.
                    patch.setitem(
                        PRINCIPALS,
                        "finance-agent",
                        replace(PRINCIPALS["finance-agent"], kind="human"),
                    )
                    with pytest.raises(
                        CommandStoreError, match="^APPROVAL_NOT_AVAILABLE$"
                    ):
                        await service.decide(ident, "finance-agent", "APPROVED", REASON)
                    patch.setitem(
                        PRINCIPALS,
                        APPROVER,
                        replace(PRINCIPALS[APPROVER], customer="C-103"),
                    )
                    with pytest.raises(
                        CommandStoreError, match="^APPROVAL_NOT_AVAILABLE$"
                    ):
                        await service.decide(ident, APPROVER, "REJECTED", REASON)
                assert (await get_state(db, ident))["approval"] == "PENDING"
                restart = await store.submit(
                    replace(
                        REQUEST,
                        tool_id="it.restart_service",
                        requester="it-agent",
                        arguments={
                            "customer_id": "C-102",
                            "service_id": "sandbox-service",
                        },
                    )
                )
                result = await service.decide(
                    restart.execution_id, "platform-admin", "APPROVED", REASON
                )
                assert result.execution_state is ExecutionState.READY
            finally:
                async with db.begin() as conn:
                    await execute(
                        conn,
                        "DELETE FROM execution_approval_entitlements WHERE principal IN ('finance-agent','platform-admin')",
                    )

    asyncio.run(check())


@DB
@pytest.mark.parametrize(
    "fault,code",
    [
        ("gate", "EXECUTION_COORDINATION_UNAVAILABLE"),
        ("guard", "EXECUTION_COORDINATION_UNAVAILABLE"),
        ("key", "EXECUTION_PAYLOAD_UNAVAILABLE"),
        ("intent", "EXECUTION_INTENT_MISMATCH"),
        ("requester", "REQUESTER_AUTHORIZATION_CHANGED"),
        ("catalog", "REQUESTER_AUTHORIZATION_CHANGED"),
    ],
)
def test_admission_fails_closed(upgraded, fault, code):
    async def check():
        async with approvals_fixture(upgraded) as (
            db,
            store,
            service,
            payloads,
            digests,
        ):
            ident = (await store.submit(REQUEST)).execution_id
            scope = ProtectedIntentScope(
                workspace_id="default",
                requester="finance-agent",
                customer_id="C-102",
                tool_id="payments.issue_refund",
            )
            commands = {
                "gate": "DELETE FROM execution_dispatch_gate",
                "guard": "DELETE FROM execution_authorization_guard",
                "requester": "UPDATE refund_grants SET enabled=false WHERE principal='finance-agent'",
                "catalog": "UPDATE tools SET lifecycle='quarantined' WHERE id='payments.issue_refund'",
            }
            if fault in commands:
                async with db.begin() as conn:
                    await execute(conn, commands[fault])
            elif fault == "intent":
                sealed = payloads.seal(
                    b'{"customer_id":"C-102","transaction_id":"different","currency":"USD","amount_minor":123}',
                    PayloadContext("command", ident, scope),
                )
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        UPDATE execution_commands SET ciphertext=:ciphertext,wrapped_data_key=:wrapped_data_key,
                            nonce=:nonce,key_version=:key_version WHERE execution_id=:id
                        """,
                        **sealed.model_dump(),
                        id=ident,
                    )
            else:
                service = ExecutionApprovals(
                    db,
                    PayloadProtection(SandboxWrappingKeys({2: urandom(32)}, 2)),
                    digests,
                    WINDOW,
                )
            try:
                with pytest.raises(CommandStoreError, match=f"^{code}$"):
                    await service.decide(ident, APPROVER, "APPROVED", REASON)
                state = await get_state(db, ident)
                assert (
                    state["approval"] == "PENDING"
                    and state["phase"] == "PENDING_APPROVAL"
                )
                assert state["evidence"] == 0 and state["audit"] == [
                    "DURABLE_APPROVAL_REQUESTED"
                ]
            finally:
                async with db.begin() as conn:
                    if fault == "requester":
                        await execute(
                            conn,
                            "UPDATE refund_grants SET enabled=true WHERE principal='finance-agent'",
                        )
                    if fault == "catalog":
                        await execute(
                            conn,
                            "UPDATE tools SET lifecycle='published' WHERE id='payments.issue_refund'",
                        )

    asyncio.run(check())


@DB
def test_concurrent_approve_reject_and_exact_duplicates(upgraded):
    async def check():
        async with approvals_fixture(upgraded) as (db, store, service, _, _):
            ident = (await store.submit(REQUEST)).execution_id
            barrier = asyncio.Barrier(12)

            async def decide(decision):
                await barrier.wait()
                try:
                    return await service.decide(ident, APPROVER, decision, REASON)
                except CommandStoreError as error:
                    assert str(error) == "APPROVAL_ALREADY_DECIDED"

            results = await asyncio.gather(
                *(decide(d) for d in ["APPROVED", "REJECTED"] * 6)
            )
            accepted = [result for result in results if result]
            assert (
                len(accepted) == 6
                and sum(not result.replayed for result in accepted) == 1
            )
            assert len({result.approval_status for result in accepted}) == 1
            state = await get_state(db, ident)
            assert len(state["audit"]) == 2 and len(state["events"]) == 2
            assert state["evidence"] == 1 and state["attempts"] == 0

    asyncio.run(check())


@DB
@pytest.mark.parametrize("race", ["expiration", "revocation"])
def test_approval_waits_for_competing_change(upgraded, race):
    async def check():
        async with approvals_fixture(upgraded) as (db, store, service, _, _):
            ident = (await store.submit(REQUEST)).execution_id
            waiting = asyncio.Event()

            @event.listens_for(db.sync_engine, "before_cursor_execute")
            def mark_wait(conn, cursor, statement, parameters, context, many):
                target = (
                    "FROM approvals WHERE id="
                    if race == "expiration"
                    else "FROM execution_authorization_guard WHERE singleton FOR SHARE"
                )
                if target in statement and "FOR " in statement:
                    waiting.set()

            try:
                async with db.begin() as conn:
                    if race == "expiration":
                        await execute(
                            conn,
                            "UPDATE approvals SET expires_at=now()-interval '1 second' WHERE id=:id",
                            id=ident,
                        )
                    else:
                        await execute(
                            conn,
                            """
                            UPDATE execution_approval_entitlements SET enabled=false
                            WHERE principal='refund-approver' AND tool_id='payments.issue_refund'
                        """,
                        )
                    pending = asyncio.create_task(
                        service.decide(ident, APPROVER, "APPROVED", REASON)
                    )
                    await asyncio.wait_for(waiting.wait(), timeout=1)
                    assert not pending.done()
                if race == "expiration":
                    expiry, result = await asyncio.gather(
                        service.expire(ident), pending
                    )
                    assert (
                        expiry.approval_status
                        is result.approval_status
                        is ApprovalState.EXPIRED
                    )
                    state = await get_state(db, ident)
                    assert state["audit"] == [
                        "DURABLE_APPROVAL_REQUESTED",
                        "DURABLE_APPROVAL_EXPIRED",
                    ]
                else:
                    with pytest.raises(
                        CommandStoreError, match="^APPROVAL_NOT_AVAILABLE$"
                    ):
                        await pending
                    state = await get_state(db, ident)
                    assert state["approval"] == "PENDING"
                assert state["evidence"] == 0
            finally:
                if race == "revocation":
                    async with db.begin() as conn:
                        await execute(
                            conn,
                            """
                            UPDATE execution_approval_entitlements SET enabled=true
                            WHERE principal='refund-approver' AND tool_id='payments.issue_refund'
                        """,
                        )

    asyncio.run(check())


@DB
def test_late_audit_failure_rolls_back_decision_and_projection(upgraded):
    async def check():
        async with approvals_fixture(upgraded) as (db, store, service, _, _):
            ident = (await store.submit(REQUEST)).execution_id
            before = await get_state(db, ident)
            async with db.begin() as conn:
                await execute(
                    conn,
                    """
                    CREATE FUNCTION reject_decision_audit() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'synthetic decision audit failure'; END $$
                """,
                )
                await execute(
                    conn,
                    """
                    CREATE TRIGGER reject_decision_audit BEFORE INSERT ON audit_events
                    FOR EACH ROW WHEN (NEW.action = 'DURABLE_APPROVAL_APPROVED')
                    EXECUTE FUNCTION reject_decision_audit()
                """,
                )
            try:
                with pytest.raises(
                    CommandStoreError, match="^APPROVAL_STORE_UNAVAILABLE$"
                ):
                    await service.decide(ident, APPROVER, "APPROVED", REASON)
                assert await get_state(db, ident) == before
            finally:
                async with db.begin() as conn:
                    await execute(
                        conn, "DROP TRIGGER reject_decision_audit ON audit_events"
                    )
                    await execute(conn, "DROP FUNCTION reject_decision_audit()")
            assert (
                await service.decide(ident, APPROVER, "APPROVED", REASON)
            ).execution_state is ExecutionState.READY

    asyncio.run(check())


@DB
def test_legacy_paths_skip_protected_approvals(upgraded):
    async def unexpected_dispatch(*args, **kwargs):
        pytest.fail("legacy adapter invoked for a protected approval")

    async def check():
        async with approvals_fixture(upgraded) as (db, store, service, _, _):
            ident = (await store.submit(REQUEST)).execution_id
            approval = Approvals(db, unexpected_dispatch, Telemetry())
            with pytest.raises(HTTPException, match="APPROVAL_NOT_AVAILABLE"):
                await approval.decide(PRINCIPALS[APPROVER], ident, True, REASON)
            assert ident not in {
                entry["id"] for entry in await approval.list(PRINCIPALS[APPROVER])
            }
            async with db.begin() as conn:
                await execute(
                    conn,
                    "UPDATE approvals SET expires_at=now()-interval '1 second' WHERE id=:id",
                    id=ident,
                )
            await approval.expire()
            assert (await get_state(db, ident))["approval"] == "PENDING"
            assert (
                await service.expire(ident)
            ).approval_status is ApprovalState.EXPIRED

    asyncio.run(check())


@DB
def test_policy_and_catalog_writers_increment_guard_without_table_grants(upgraded):
    async def check():
        async with approvals_fixture(upgraded) as (db, store, _, _, _):
            ident = (await store.submit(REQUEST)).execution_id
            original = (await get_state(db, ident))["revision"]
            async with db.begin() as conn:
                await execute(conn, "SET LOCAL ROLE toolmesh_app")
                [access] = await rows(
                    conn,
                    """
                    SELECT has_table_privilege(current_user,'execution_approval_decisions','SELECT') AS evidence,
                           has_table_privilege(current_user,'execution_authorization_guard','UPDATE') AS guard
                """,
                )
                assert access == {"evidence": False, "guard": False}
                await execute(conn, "SELECT public.lock_execution_authorization()")
                await execute(
                    conn,
                    "UPDATE agent_accounts SET enabled=enabled WHERE id='finance-agent'",
                )
                await execute(
                    conn,
                    "UPDATE policy_rules SET enabled=enabled WHERE principal='finance-agent'",
                )
                await execute(
                    conn,
                    "UPDATE tools SET active_version=active_version WHERE id='payments.issue_refund'",
                )
            assert (await get_state(db, ident))["revision"] == original + 3

    asyncio.run(check())


@DB
def test_revocation_after_admission_cannot_rewrite_approved_revision(
    upgraded, monkeypatch
):
    async def check():
        async with approvals_fixture(upgraded) as (db, store, service, _, _):
            ident = (await store.submit(REQUEST)).execution_id
            admitted = asyncio.Event()
            finish_decision = asyncio.Event()
            writer_started = asyncio.Event()
            authorize = service._authorize_requester

            async def pause_after_check(*args):
                await authorize(*args)
                admitted.set()
                await finish_decision.wait()

            @event.listens_for(db.sync_engine, "before_cursor_execute")
            def mark_writer(conn, cursor, statement, parameters, context, many):
                if "UPDATE refund_grants SET enabled=false" in statement:
                    writer_started.set()

            async def revoke():
                async with db.begin() as conn:
                    await execute(
                        conn,
                        "UPDATE refund_grants SET enabled=false WHERE principal='finance-agent'",
                    )

            try:
                with monkeypatch.context() as patch:
                    patch.setattr(service, "_authorize_requester", pause_after_check)
                    async with asyncio.TaskGroup() as group:
                        decision = group.create_task(
                            service.decide(ident, APPROVER, "APPROVED", REASON)
                        )
                        await asyncio.wait_for(admitted.wait(), timeout=2)
                        revoked = group.create_task(revoke())
                        await asyncio.wait_for(writer_started.wait(), timeout=2)
                        assert not revoked.done()
                        finish_decision.set()
                assert decision.result().execution_state is ExecutionState.READY
                async with db.connect() as conn:
                    [evidence] = await rows(
                        conn,
                        "SELECT authorization_revision FROM execution_approval_decisions WHERE execution_id=:id",
                        id=ident,
                    )
                state = await get_state(db, ident)
                assert state["revision"] == evidence["authorization_revision"] + 1
                assert state["attempts"] == 0
            finally:
                async with db.begin() as conn:
                    await execute(
                        conn,
                        "UPDATE refund_grants SET enabled=true WHERE principal='finance-agent'",
                    )

    asyncio.run(check())
