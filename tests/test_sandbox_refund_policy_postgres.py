"""Durable verifier windows and operator attention on both PostgreSQL baselines."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncTransaction, create_async_engine
from test_durable_execution_schema import IMAGE
from test_durable_execution_schema import sandbox as sandbox
from test_execution_worker_lifecycle_postgres import (
    event_from,
    start_worker,
    stop_worker,
)
from test_execution_worker_postgres import expire_synthetic_lease, snapshot
from test_sandbox_refund_worker_postgres import PASSWORD, key_setting, worker_login
from test_synthetic_refund_postgres import effect
from test_synthetic_refund_postgres import upgraded as upgraded
from test_synthetic_refund_reconciliation_postgres import (
    check_terminal,
    make_due,
    parked,
)

from gateway.execution_contracts import ExecutionState
from gateway.execution_store import CommandStoreError
from gateway.sandbox_refund_evidence import RefundIntent, RefundRequest
from gateway.sandbox_refund_reconciliation import RefundReconciler
from gateway.sandbox_refund_store import SandboxRefundStore
from shared.database import execute, rows

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires disposable PostgreSQL image"
)
OPERATOR = "m914_attention_test"


async def configure(
    db, *, attempts=4, elapsed=60000, initial=200, cap=500, jitter=0, lookup=3000
):
    async with db.begin() as conn:
        await execute(
            conn,
            """
            UPDATE m914_refund.retry_policy SET max_attempts=:attempts,
                elapsed_ms=:elapsed,initial_ms=:initial,max_delay_ms=:cap,
                jitter_percent=:jitter,lookup_ms=:lookup WHERE singleton
            """,
            attempts=attempts,
            elapsed=elapsed,
            initial=initial,
            cap=cap,
            jitter=jitter,
            lookup=lookup,
        )


@asynccontextmanager
async def operator_login(db):
    async with db.begin() as conn:
        await execute(
            conn,
            f"CREATE ROLE {OPERATOR} LOGIN PASSWORD '{PASSWORD}' IN ROLE m914_refund_operator",
        )
        await execute(conn, f"GRANT CONNECT ON DATABASE toolmesh TO {OPERATOR}")
    engine = create_async_engine(
        db.url.set(username=OPERATOR, password=PASSWORD), hide_parameters=True
    )
    try:
        yield engine
    finally:
        await engine.dispose()
        async with db.begin() as conn:
            await execute(conn, f"DROP OWNED BY {OPERATOR}")
            await execute(conn, f"DROP ROLE {OPERATOR}")


async def attention(engine, ident):
    async with engine.connect() as conn:
        return await rows(conn, "SELECT * FROM m914_refund.attention(:id)", id=ident)


async def resume(engine, ident, window):
    async with engine.begin() as conn:
        return await rows(
            conn,
            "SELECT * FROM m914_refund.resume(:id,:window,:reason)",
            id=ident,
            window=window,
            reason="Checked synthetic ledger availability",
        )


async def intended(store, claim):
    scope, args, sealed_digest = await store.read_intent(claim)
    intent = RefundIntent(
        claim.sender, scope, RefundRequest.model_validate(args), sealed_digest
    )
    assert intent.binding == claim.intent_binding
    return intent


async def scheduled_delay(db, ident):
    async with db.connect() as conn:
        [record] = await rows(
            conn,
            """
            SELECT round(extract(epoch FROM c.next_check_at-a.finished_at)*1000)::int AS ms,
                   c.next_check_at=w.deadline AS at_deadline,w.deadline
            FROM execution_commands c JOIN execution_attempts a USING (execution_id)
            JOIN m914_refund.windows w USING (execution_id)
            WHERE c.execution_id=:id AND a.generation=c.generation
        """,
            id=ident,
        )
    return record


async def assert_paused(db, store, sender, send, cause, count):
    ident = sender.execution_id
    state, attempts = await snapshot(db, ident)
    assert state["state"] == state["status"] == "UNKNOWN"
    assert state["receipts"] == 0 and attempts[0] == send
    assert (
        state["lease_owner"] is None and state["command_row"]["next_check_at"] is None
    )
    assert len(attempts) == count + 1
    assert all(
        a["kind"] == "VERIFY" and a["outcome"] == "UNKNOWN" for a in attempts[1:]
    )
    pauses = [
        row
        for row in state["audit_rows"]
        if row["action"].startswith("SYNTHETIC_VERIFY_ATTENTION_")
    ]
    assert [row["action"] for row in pauses] == [f"SYNTHETIC_VERIFY_ATTENTION_{cause}"]
    assert await store.discover_due_synthetic_refunds() == []
    assert await store.claim_synthetic_refund_verification(ident, uuid4()) is None
    return attempts


@pytest.mark.parametrize("jitter", [0, 25])
def test_backoff_attempt_budget_and_authorized_resume(upgraded, monkeypatch, jitter):
    async def check():
        async with parked(upgraded, monkeypatch, committed=False) as fixture:
            db, base, ledger, approvals, flow, sender, send, lookup, _, keys = fixture
            ident = sender.execution_id
            async with worker_login(db) as engine, operator_login(db) as operator:
                await configure(db, jitter=jitter)
                store = SandboxRefundStore(
                    engine, approvals, timedelta(seconds=20), keys[-1]
                )
                for index, nominal in enumerate((200, 400, 500, None), start=1):
                    assert await store.discover_due_synthetic_refunds() == [ident]
                    claims = await asyncio.gather(
                        *(
                            store.claim_synthetic_refund_verification(ident, uuid4())
                            for _ in range(2)
                        )
                    )
                    [claim] = [claim for claim in claims if claim is not None]
                    assert claim.sender == sender
                    assert claim.lease.owner.generation == sender.generation + index
                    intent = await intended(store, claim)
                    assert (
                        await store.finish_synthetic_refund_verification(claim)
                        is ExecutionState.UNKNOWN
                    )
                    if nominal is not None:
                        fence = claim.lease.owner.generation
                        byte = sha256(ident.bytes + fence.to_bytes(8, "big")).digest()[
                            0
                        ]
                        expected = nominal - nominal * jitter * byte // 25500
                        assert (await scheduled_delay(db, ident))["ms"] == expected
                        await make_due(db, ident)
                original_attempts = await assert_paused(
                    db, store, sender, send, "ATTEMPTS", 4
                )
                assert await effect(db, ident) == (0, None, 500)
                [record] = await attention(operator, ident)
                assert record["window_number"] == 1
                assert record["attempts_used"] == 4 and record["reason"] == "ATTEMPTS"
                with pytest.raises(
                    CommandStoreError,
                    match="REFUND_VERIFICATION_RESTRICTED_INTERFACE_REQUIRED",
                ):
                    await base.claim_synthetic_refund_verification(ident, uuid4())
                with pytest.raises(DBAPIError) as denied:
                    await resume(engine, ident, 1)
                assert denied.value.orig.sqlstate == "42501"
                with pytest.raises(DBAPIError) as denied:
                    async with engine.begin() as conn:
                        await rows(
                            conn,
                            """
                            SELECT * FROM m914_refund.operate('resume',:id,NULL,1,NULL,NULL,NULL,'bypass')
                        """,
                            id=ident,
                        )
                assert denied.value.orig.sqlstate == "42501"
                with pytest.raises(DBAPIError) as denied:
                    async with operator.begin() as conn:
                        await rows(conn, "SELECT * FROM m914_refund.discover(1)")
                assert denied.value.orig.sqlstate == "42501"

                assert await resume(operator, ident, 2) == []
                resumes = await asyncio.gather(
                    resume(operator, ident, 1), resume(operator, ident, 1)
                )
                assert resumes.count([{"window_number": 2}]) == resumes.count([]) == 1
                running, history = await snapshot(db, ident)
                assert (
                    history == original_attempts
                    and running["generation"] == claim.lease.owner.generation
                )
                assert running["state"] == running["status"] == "UNKNOWN"
                [event] = [
                    row
                    for row in running["audit_rows"]
                    if row["action"].startswith("SYNTHETIC_VERIFY_RESUMED:")
                ]
                assert (
                    event["actor"] == OPERATOR
                    and "Checked synthetic ledger availability" in event["action"]
                )
                assert await attention(operator, ident) == []
                assert await store.finish_synthetic_refund_verification(claim) is None
                assert await flow.run_once(uuid4()) is None
                assert await effect(db, ident) == (0, None, 500)
                # Deliver the original request. Resume grants permission to look up only.
                await ledger.invoke(intent)
                reconciler = RefundReconciler(approvals, store, lookup)
                assert (
                    await reconciler.run_once(ident, uuid4())
                    is ExecutionState.SUCCEEDED
                )
                await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
                assert (await snapshot(db, ident))[1][:-1] == original_attempts
                assert await effect(db, ident) == (1, 123, 377)

    asyncio.run(check())


def test_deadline_clamps_schedule_and_exhausts_without_another_attempt(
    upgraded, monkeypatch
):
    async def check():
        async with parked(upgraded, monkeypatch, committed=False) as fixture:
            db, _, _, approvals, _, sender, send, _, _, keys = fixture
            ident = sender.execution_id
            async with worker_login(db) as engine, operator_login(db) as operator:
                await configure(
                    db, elapsed=30000, initial=60000, cap=60000, lookup=30000
                )
                store = SandboxRefundStore(
                    engine, approvals, timedelta(seconds=60), keys[-1]
                )
                claim = await store.claim_synthetic_refund_verification(ident, uuid4())
                assert claim is not None and claim.lookup_timeout_ms < 30000
                assert (
                    await store.finish_synthetic_refund_verification(claim)
                    is ExecutionState.UNKNOWN
                )
                schedule = await scheduled_delay(db, ident)
                assert (
                    schedule["at_deadline"]
                    and schedule["deadline"] == claim.lease.expires_at
                )
                assert await store.discover_due_synthetic_refunds() == []
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        UPDATE m914_refund.windows SET deadline=clock_timestamp()-interval '1 second' WHERE execution_id=:id
                    """,
                        id=ident,
                    )
                    await execute(
                        conn,
                        """
                        UPDATE execution_commands SET next_check_at=clock_timestamp()-interval '1 second' WHERE execution_id=:id
                    """,
                        id=ident,
                    )
                assert await store.discover_due_synthetic_refunds() == [ident]
                assert (
                    await store.claim_synthetic_refund_verification(ident, uuid4())
                    is None
                )
                await assert_paused(db, store, sender, send, "DEADLINE", 1)
                [record] = await attention(operator, ident)
                assert record["reason"] == "DEADLINE" and record["attempts_used"] == 1
                assert await effect(db, ident) == (0, None, 500)

    asyncio.run(check())


def test_budget_survives_interrupted_worker_and_policy_change(upgraded, monkeypatch):
    async def check():
        async with parked(upgraded, monkeypatch) as fixture:
            db, _, _, approvals, _, sender, send, _, _, keys = fixture
            ident = sender.execution_id
            async with worker_login(db) as engine, operator_login(db) as operator:
                await configure(db, attempts=1)
                program = """
import asyncio
from gateway import execution_worker as worker
async def hold(*args):
    print('holding_lookup', flush=True)
    await asyncio.Event().wait()
worker.read_refund_evidence = hold
raise SystemExit(worker.main())
"""
                proc = await start_worker(
                    engine,
                    program=(program,),
                    drain_seconds=0.2,
                    refund_verification_keys=key_setting(keys),
                )
                try:
                    await event_from(proc, b"holding_lookup")
                finally:
                    assert await stop_worker(proc) == 0
                before, [original, interrupted] = await snapshot(db, ident)
                assert original == send and interrupted["outcome"] is None
                assert before["state"] == "UNKNOWN" and before["receipts"] == 0
                await configure(db, attempts=6)
                await expire_synthetic_lease(db, ident)
                workers = await asyncio.gather(
                    *(
                        start_worker(engine, refund_verification_keys=key_setting(keys))
                        for _ in range(2)
                    )
                )
                try:
                    async with asyncio.timeout(10):
                        while not (observed := await attention(operator, ident)):
                            await asyncio.sleep(0.05)
                    assert (
                        observed[0]["reason"] == "ATTEMPTS"
                        and observed[0]["attempts_used"] == 1
                    )
                    store = SandboxRefundStore(
                        engine, approvals, timedelta(seconds=5), keys[-1]
                    )
                    await assert_paused(db, store, sender, send, "ATTEMPTS", 1)
                    assert await effect(db, ident) == (1, 123, 377)
                finally:
                    assert await asyncio.gather(
                        *(stop_worker(proc) for proc in workers)
                    ) == [0, 0]

    asyncio.run(check())


def test_configured_lookup_timeout_defers_without_terminal_effect(
    upgraded, monkeypatch
):
    async def check():
        async with parked(upgraded, monkeypatch, committed=False) as fixture:
            db, _, ledger, approvals, _, sender, send, lookup, _, keys = fixture
            ident = sender.execution_id
            async with worker_login(db) as engine:
                await configure(db, lookup=250)
                store = SandboxRefundStore(
                    engine, approvals, timedelta(seconds=10), keys[-1]
                )
                observed = []

                async def slow(intent):
                    observed.append(intent)
                    await asyncio.Event().wait()

                reconciler = RefundReconciler(approvals, store, slow)
                async with asyncio.timeout(5):
                    assert (
                        await reconciler.run_once(ident, uuid4())
                        is ExecutionState.UNKNOWN
                    )
                assert len(observed) == 1
                state, [original, verify] = await snapshot(db, ident)
                assert original == send and verify["outcome"] == "UNKNOWN"
                assert (
                    state["audit_rows"][-1]["action"]
                    == "SYNTHETIC_VERIFY_DEFERRED_TIMEOUT"
                )
                assert state["receipts"] == 0 and await effect(db, ident) == (
                    0,
                    None,
                    500,
                )
                assert (await scheduled_delay(db, ident))["ms"] == 200
                await ledger.invoke(observed[0])
                await make_due(db, ident)
                assert (
                    await RefundReconciler(approvals, store, lookup).run_once(
                        ident, uuid4()
                    )
                    is ExecutionState.SUCCEEDED
                )
                await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
                assert await effect(db, ident) == (1, 123, 377)

    asyncio.run(check())


def test_policy_counts_history_from_before_privilege_provisioning(
    upgraded, monkeypatch
):
    async def check():
        async with parked(upgraded, monkeypatch, committed=False) as fixture:
            db, base, _, approvals, _, sender, send, lookup, _, keys = fixture
            ident = sender.execution_id
            assert (
                await RefundReconciler(approvals, base, lookup).run_once(ident, uuid4())
                is ExecutionState.UNKNOWN
            )
            async with worker_login(db) as engine:
                await configure(db, attempts=1)
                await make_due(db, ident)
                store = SandboxRefundStore(
                    engine, approvals, timedelta(seconds=5), keys[-1]
                )
                assert (
                    await store.claim_synthetic_refund_verification(ident, uuid4())
                    is None
                )
                await assert_paused(db, store, sender, send, "ATTEMPTS", 1)
                assert await effect(db, ident) == (0, None, 500)

    asyncio.run(check())


def test_deadline_fences_late_committed_evidence(upgraded, monkeypatch):
    async def check():
        async with parked(upgraded, monkeypatch) as fixture:
            db, _, _, approvals, _, sender, send, lookup, intents, keys = fixture
            ident = sender.execution_id
            async with worker_login(db) as engine:
                await configure(db, elapsed=30000)
                store = SandboxRefundStore(
                    engine, approvals, timedelta(seconds=60), keys[-1]
                )
                claim = await store.claim_synthetic_refund_verification(ident, uuid4())
                assert claim is not None
                assert (await scheduled_delay(db, ident))[
                    "deadline"
                ] == claim.lease.expires_at
                evidence = await lookup(intents[0])
                assert evidence is not None
                # Advance past both the deadline and its capped lease using DB time.
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        UPDATE m914_refund.windows SET deadline=statement_timestamp()-interval '1 second'
                        WHERE execution_id=:id
                    """,
                        id=ident,
                    )
                    await execute(
                        conn,
                        """
                        UPDATE execution_commands c SET lease_expires_at=w.deadline
                        FROM m914_refund.windows w WHERE c.execution_id=:id AND w.execution_id=c.execution_id
                    """,
                        id=ident,
                    )
                assert (
                    await store.finish_synthetic_refund_verification(
                        claim,
                        evidence=evidence,
                        binding_preimage=intents[0].binding_preimage(),
                    )
                    is None
                )
                assert (
                    await store.claim_synthetic_refund_verification(ident, uuid4())
                    is None
                )
                await assert_paused(db, store, sender, send, "DEADLINE", 1)
                assert await effect(db, ident) == (1, 123, 377)

    asyncio.run(check())


@pytest.mark.parametrize("fault", ["rollback", "lost-ack"])
def test_attention_transaction_is_atomic(upgraded, monkeypatch, fault):
    async def check():
        async with parked(upgraded, monkeypatch, committed=False) as fixture:
            db, _, _, approvals, _, sender, send, _, _, keys = fixture
            ident = sender.execution_id
            async with worker_login(db) as engine, operator_login(db) as operator:
                await configure(db, attempts=1)
                store = SandboxRefundStore(
                    engine, approvals, timedelta(seconds=20), keys[-1]
                )
                claim = await store.claim_synthetic_refund_verification(ident, uuid4())
                assert claim is not None
                original_exit = AsyncTransaction.__aexit__

                async def fault_exit(transaction, *failure):
                    assert failure[0] is None
                    if fault == "rollback":
                        await transaction.rollback()
                    else:
                        await original_exit(transaction, *failure)
                    raise OSError("fixture transaction fault")

                with monkeypatch.context() as patch:
                    patch.setattr(AsyncTransaction, "__aexit__", fault_exit)
                    with pytest.raises(
                        CommandStoreError, match="EXECUTION_OWNERSHIP_UNAVAILABLE"
                    ):
                        await store.finish_synthetic_refund_verification(claim)
                if fault == "rollback":
                    assert await attention(operator, ident) == []
                    state, [original, verifier] = await snapshot(db, ident)
                    assert original == send and verifier["outcome"] is None
                    assert state["lease_owner"] is not None
                    await expire_synthetic_lease(db, ident)
                else:
                    assert (await attention(operator, ident))[0]["reason"] == "ATTEMPTS"
                assert (
                    await store.claim_synthetic_refund_verification(ident, uuid4())
                    is None
                )
                await assert_paused(db, store, sender, send, "ATTEMPTS", 1)
                assert await effect(db, ident) == (0, None, 500)

    asyncio.run(check())


@asynccontextmanager
async def audit_logins(db):
    """Exercise native app ACLs and inherited grants without SET ROLE authority."""
    inherited = "m914_audit_inheritor"
    async with db.begin() as conn:
        await execute(
            conn,
            f"CREATE ROLE {inherited} LOGIN PASSWORD '{PASSWORD}' IN ROLE toolmesh_app",
        )
        await execute(
            conn,
            f"GRANT m914_refund_function_owner TO {inherited} WITH INHERIT TRUE, SET FALSE",
        )
        await execute(
            conn,
            f"GRANT UPDATE, DELETE, TRUNCATE ON public.audit_events TO {inherited}",
        )
    app = create_async_engine(
        db.url.set(username="toolmesh_app", password="synthetic-app-test"),
        hide_parameters=True,
    )
    inheritor = create_async_engine(
        db.url.set(username=inherited, password=PASSWORD), hide_parameters=True
    )
    try:
        yield app, inheritor
    finally:
        await app.dispose()
        await inheritor.dispose()
        async with db.begin() as conn:
            await execute(conn, f"DROP OWNED BY {inherited}")
            await execute(conn, f"DROP ROLE {inherited}")


def test_operator_audit_is_reserved_to_the_effective_function_owner(
    upgraded, monkeypatch
):
    async def check():
        async with parked(upgraded, monkeypatch, committed=False) as fixture:
            db, _, _, approvals, _, sender, _, _, _, keys = fixture
            ident = sender.execution_id
            async with (
                worker_login(db) as engine,
                operator_login(db) as operator,
                audit_logins(db) as (app, inheritor),
            ):
                await configure(db, attempts=1)
                store = SandboxRefundStore(
                    engine, approvals, timedelta(seconds=20), keys[-1]
                )
                insert = """
                    INSERT INTO public.audit_events(actor,action,resource_id)
                    VALUES (:actor,:action,:id)
                """
                async with inheritor.connect() as conn:
                    [identity] = await rows(
                        conn,
                        """
                        SELECT current_user, session_user,
                            pg_has_role(current_user,'m914_refund_function_owner','USAGE') AS inherits,
                            pg_has_role(current_user,'m914_refund_function_owner','SET') AS can_set
                    """,
                    )
                assert identity == {
                    "current_user": "m914_audit_inheritor",
                    "session_user": "m914_audit_inheritor",
                    "inherits": True,
                    "can_set": False,
                }
                for login in (app, inheritor):
                    for action in (
                        "SYNTHETIC_VERIFY_RESUMED",
                        "SYNTHETIC_VERIFY_RESUMED:2:4:fake reason",
                        "SYNTHETIC_VERIFY_ATTENTION_ATTEMPTS",
                        "SYNTHETIC_VERIFY_ATTENTION_DEADLINE",
                    ):
                        with pytest.raises(DBAPIError) as denied:
                            async with login.begin() as conn:
                                await execute(
                                    conn,
                                    insert,
                                    actor=OPERATOR,
                                    action=action,
                                    id=ident,
                                )
                        assert denied.value.orig.sqlstate == "42501"
                        assert "refund operator audit action is reserved" in str(
                            denied.value.orig
                        )

                async with app.begin() as conn:
                    await execute(
                        conn,
                        insert,
                        actor="ordinary-app",
                        action="ORDINARY_AUDIT",
                        id=ident,
                    )

                claim = await store.claim_synthetic_refund_verification(ident, uuid4())
                assert claim is not None
                assert (
                    await store.finish_synthetic_refund_verification(claim)
                    is ExecutionState.UNKNOWN
                )
                assert await resume(operator, ident, 1) == [{"window_number": 2}]
                before, attempts = await snapshot(db, ident)
                [ordinary] = [
                    event
                    for event in before["audit_rows"]
                    if event["action"] == "ORDINARY_AUDIT"
                ]
                [paused] = [
                    event
                    for event in before["audit_rows"]
                    if event["action"] == "SYNTHETIC_VERIFY_ATTENTION_ATTEMPTS"
                ]
                [resumed] = [
                    event
                    for event in before["audit_rows"]
                    if event["action"].startswith("SYNTHETIC_VERIFY_RESUMED:")
                ]
                assert paused["actor"] == str(claim.lease.owner.worker_id)
                assert resumed["actor"] == OPERATOR
                assert resumed["action"] == (
                    f"SYNTHETIC_VERIFY_RESUMED:2:{claim.lease.owner.generation}:"
                    "Checked synthetic ledger availability"
                )

                for statement, event_id in (
                    (
                        "UPDATE public.audit_events SET action='SYNTHETIC_VERIFY_RESUMED' WHERE id=:id",
                        ordinary["id"],
                    ),
                    (
                        "UPDATE public.audit_events SET actor='forged' WHERE id=:id",
                        paused["id"],
                    ),
                    (
                        "UPDATE public.audit_events SET action='ORDINARY_AUDIT' WHERE id=:id",
                        resumed["id"],
                    ),
                    ("DELETE FROM public.audit_events WHERE id=:id", paused["id"]),
                    ("DELETE FROM public.audit_events WHERE id=:id", resumed["id"]),
                ):
                    with pytest.raises(DBAPIError) as denied:
                        async with inheritor.begin() as conn:
                            await execute(conn, statement, id=event_id)
                    assert denied.value.orig.sqlstate == "42501"
                    assert "refund operator audit events are protected" in str(
                        denied.value.orig
                    )
                with pytest.raises(DBAPIError) as denied:
                    async with inheritor.begin() as conn:
                        await execute(conn, "TRUNCATE public.audit_events")
                assert denied.value.orig.sqlstate == "42501"
                assert await snapshot(db, ident) == (before, attempts)
                async with inheritor.begin() as conn:
                    await execute(
                        conn,
                        """
                        UPDATE public.audit_events SET actor='updated-ordinary-app' WHERE id=:id
                    """,
                        id=ordinary["id"],
                    )
                    await execute(
                        conn,
                        "DELETE FROM public.audit_events WHERE id=:id",
                        id=ordinary["id"],
                    )
                assert await effect(db, ident) == (0, None, 500)

    asyncio.run(check())


def test_operator_audit_guard_is_required_before_verification_mutates(
    upgraded, monkeypatch
):
    async def check():
        async with parked(upgraded, monkeypatch, committed=False) as fixture:
            db, _, _, approvals, _, sender, _, _, _, keys = fixture
            ident = sender.execution_id
            async with worker_login(db) as engine, operator_login(db) as operator:
                await configure(db, attempts=1)
                store = SandboxRefundStore(
                    engine, approvals, timedelta(seconds=20), keys[-1]
                )
                claim = await store.claim_synthetic_refund_verification(ident, uuid4())
                assert claim is not None
                before = await snapshot(db, ident)
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        ALTER TABLE public.audit_events DISABLE TRIGGER protect_synthetic_refund_operator_rows
                    """,
                    )
                try:
                    with pytest.raises(
                        CommandStoreError,
                        match="SYNTHETIC_OPERATOR_AUDIT_PROTECTION_UNAVAILABLE",
                    ):
                        await store.finish_synthetic_refund_verification(claim)
                    assert await snapshot(db, ident) == before
                finally:
                    async with db.begin() as conn:
                        await execute(
                            conn,
                            """
                            ALTER TABLE public.audit_events ENABLE ALWAYS TRIGGER protect_synthetic_refund_operator_rows
                        """,
                        )
                assert (
                    await store.finish_synthetic_refund_verification(claim)
                    is ExecutionState.UNKNOWN
                )
                assert await resume(operator, ident, 1) == [{"window_number": 2}]

    asyncio.run(check())
