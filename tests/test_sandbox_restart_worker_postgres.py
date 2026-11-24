"""Fenced restart verification on both disposable PostgreSQL baselines."""

import asyncio
import signal
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from hmac import digest
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncTransaction
from synthetic_it_restart import TOOL
from synthetic_refund_flow import RefundFlow
from test_durable_execution_schema import IMAGE
from test_durable_execution_schema import sandbox as sandbox
from test_execution_store import REQUEST
from test_execution_worker_lifecycle_postgres import (
    event_from,
    start_worker,
    stop_worker,
)
from test_execution_worker_postgres import expire_synthetic_lease, snapshot
from test_execution_worker_postgres import upgraded as upgraded
from test_sandbox_refund_policy_postgres import (
    OPERATOR,
    attention,
    audit_logins,
    configure,
    operator_login,
    resume,
)
from test_sandbox_refund_worker_postgres import key_setting, worker_login
from test_synthetic_it_restart_postgres import effects, restart_case
from test_synthetic_refund_postgres import current_owner, effect, services
from test_synthetic_refund_reconciliation_postgres import (
    SimulatedCrash,
    check_terminal,
    make_due,
)

from gateway.execution_contracts import ExecutionState
from gateway.execution_store import CommandStoreError
from gateway.execution_worker_store import synthetic_evidence_bytes
from gateway.sandbox_refund_reconciliation import RestartReconciler
from gateway.sandbox_refund_store import SandboxRestartStore
from shared.database import execute, rows

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires disposable PostgreSQL image"
)


async def parked_send(
    db, ownership, adapter, flow, ident, monkeypatch, *, committed=True, restart=True
):
    intents = []

    async def crash(intent):
        intents.append(intent)
        raise SimulatedCrash

    async def interrupt():
        raise SimulatedCrash

    lookup = adapter.evidence
    with monkeypatch.context() as patch:
        patch.setattr(adapter, "evidence", crash)
        with pytest.raises(SimulatedCrash):
            await flow.run_once(
                uuid4(), after_consumption=None if committed else interrupt
            )
    sender = await current_owner(db, ident)
    park = (
        ownership.recover_expired_synthetic_restart
        if restart
        else ownership.recover_expired_synthetic_refund
    )
    assert await park(sender) is False
    await expire_synthetic_lease(db, ident)
    assert await park(sender)
    assert not await park(sender)
    state, [send] = await snapshot(db, ident)
    assert state["state"] == state["status"] == "UNKNOWN"
    assert send["kind"] == "SEND" and send["outcome"] == "UNKNOWN"
    if restart:
        event = state["audit_rows"][-1]
        assert event["actor"] == str(sender.worker_id)
        assert event["action"] == "SYNTHETIC_RESTART_SEND_PARKED"
    return sender, send, lookup, intents


@asynccontextmanager
async def parked_restart(upgraded, monkeypatch, *, allowed=True, committed=True):
    async with restart_case(upgraded, allowed=allowed) as fixture:
        db, approvals, ownership, adapter, flow, ident, keys = fixture
        sender, send, lookup, intents = await parked_send(
            db, ownership, adapter, flow, ident, monkeypatch, committed=committed
        )
        _, _, _, refund = services(db, keys)
        await refund.initialize()
        try:
            yield (
                db,
                approvals,
                ownership,
                adapter,
                flow,
                sender,
                send,
                lookup,
                intents,
                refund,
                keys,
            )
        finally:
            await refund.close()


def test_restart_worker_success_and_canonical_readback(upgraded, monkeypatch):
    async def check():
        async with parked_restart(upgraded, monkeypatch) as fixture:
            db, _, _, _, _, sender, send, _, _, _, keys = fixture
            ident = sender.execution_id
            async with worker_login(db, refund=False, restart=True) as engine:
                worker = await start_worker(
                    engine, restart_verification_keys=key_setting(keys)
                )
                try:
                    assert b"SUCCEEDED" in await event_from(worker, b"restart_verified")
                    await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
                    assert await effects(db, ident) == (1, 1, 8)
                finally:
                    assert await stop_worker(worker) == 0

    asyncio.run(check())


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("fault", ["lost-ack", "rollback"])
def test_restart_receipt_transaction_recovery(upgraded, monkeypatch, allowed, fault):
    async def check():
        async with parked_restart(upgraded, monkeypatch, allowed=allowed) as fixture:
            db, approvals, _, adapter, _, sender, send, lookup, intents, _, keys = (
                fixture
            )
            ident = sender.execution_id
            expected = ExecutionState.SUCCEEDED if allowed else ExecutionState.FAILED
            async with worker_login(db, refund=False, restart=True) as engine:
                store = SandboxRestartStore(
                    engine, approvals, timedelta(seconds=20), keys[-1]
                )
                original = store.finish_synthetic_refund_verification
                original_exit = AsyncTransaction.__aexit__

                async def fail_exit(transaction, *failure):
                    assert failure[0] is None
                    if fault == "lost-ack":
                        await original_exit(transaction, *failure)
                    else:
                        await transaction.rollback()
                    raise OSError("transaction interrupted")

                async def finish(*args, **kwargs):
                    with monkeypatch.context() as patch:
                        patch.setattr(AsyncTransaction, "__aexit__", fail_exit)
                        return await original(*args, **kwargs)

                reconciler = RestartReconciler(approvals, store, lookup)
                with monkeypatch.context() as patch:
                    patch.setattr(store, "finish_synthetic_refund_verification", finish)
                    if fault == "lost-ack":
                        assert await reconciler.run_once(ident, uuid4()) is expected
                    else:
                        with pytest.raises(
                            CommandStoreError, match="EXECUTION_OWNERSHIP_UNAVAILABLE"
                        ):
                            await reconciler.run_once(ident, uuid4())
                if fault == "rollback":
                    state, [original_send, attempt] = await snapshot(db, ident)
                    assert original_send == send and attempt["outcome"] is None
                    assert state["state"] == "UNKNOWN" and state["receipts"] == 0
                    assert await reconciler.run_once(ident, uuid4()) is None
                    await expire_synthetic_lease(db, ident)
                    assert await reconciler.run_once(ident, uuid4()) is expected
                await check_terminal(db, sender, send, expected)
                state, _ = await snapshot(db, ident)
                evidence = await lookup(intents[0])
                assert evidence is not None
                assert evidence.effect_version == (8 if allowed else None)
                assert await reconciler.run_once(ident, uuid4()) is None
                assert state["receipts"] == 1
                assert await effects(db, ident) == ((1, 1, 8) if allowed else (1, 0, 7))
                if not allowed:
                    async with db.begin() as conn:
                        await execute(
                            conn,
                            "UPDATE synthetic_restart.services SET restart_allowed=true",
                        )
                    with pytest.raises(
                        CommandStoreError, match="SYNTHETIC_RESTART_NOT_AUTHORIZED"
                    ):
                        await adapter.invoke(intents[0])
                    assert await effects(db, ident) == (2, 0, 7)
                    assert (await snapshot(db, ident))[0] == state

    asyncio.run(check())


@pytest.mark.parametrize("mismatch", ["tenant", "service", "attempt", "boot"])
def test_mismatched_restart_evidence_preserves_unknown(upgraded, monkeypatch, mismatch):
    async def check():
        async with parked_restart(upgraded, monkeypatch) as fixture:
            db, approvals, _, _, _, sender, send, lookup, intents, _, keys = fixture
            ident = sender.execution_id
            [intent] = intents
            valid = await lookup(intent)
            assert valid is not None
            async with worker_login(db, refund=False, restart=True) as engine:
                store = SandboxRestartStore(
                    engine, approvals, timedelta(seconds=20), keys[-1]
                )
                # Exercise SQL validation directly, independent of the read-side checks.
                claim = await store.claim_synthetic_refund_verification(ident, uuid4())
                assert claim is not None
                fields = {
                    "tenant": ("customer_id", intent.scope.customer_id, "wrong-tenant"),
                    "service": (
                        "service_id",
                        intent.request.service_id,
                        "wrong-service",
                    ),
                    "attempt": (
                        "sender_generation",
                        sender.generation,
                        sender.generation + 1,
                    ),
                }

                async def rewrite(value):
                    column = fields[mismatch][0]
                    async with db.begin() as conn:
                        await execute(
                            conn,
                            f"UPDATE synthetic_restart.outcomes SET {column}=:value WHERE operation_id=:id",
                            value=value,
                            id=ident,
                        )

                candidate = valid
                if mismatch == "boot":
                    candidate = valid.model_copy(update={"effect_version": 9})
                    candidate = candidate.model_copy(
                        update={
                            "attestation": digest(
                                keys[-1], synthetic_evidence_bytes(candidate), "sha256"
                            )
                        }
                    )
                else:
                    await rewrite(fields[mismatch][2])
                    assert await lookup(intent) is None
                with pytest.raises(
                    CommandStoreError, match="SYNTHETIC_EVIDENCE_MISMATCH"
                ):
                    await store.finish_synthetic_refund_verification(
                        claim,
                        evidence=candidate,
                        binding_preimage=intent.binding_preimage(),
                    )
                assert (
                    await store.finish_synthetic_refund_verification(
                        claim, deferral_reason="MISMATCH"
                    )
                    is ExecutionState.UNKNOWN
                )
                state, [original, attempt] = await snapshot(db, ident)
                assert (
                    state["receipts"] == 0
                    and original == send
                    and attempt["outcome"] == "UNKNOWN"
                )
                if mismatch != "boot":
                    await rewrite(fields[mismatch][1])
                await make_due(db, ident)
                assert (
                    await RestartReconciler(approvals, store, lookup).run_once(
                        ident, uuid4()
                    )
                    is ExecutionState.SUCCEEDED
                )
                await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
                assert await effects(db, ident) == (1, 1, 8)

    asyncio.run(check())


@pytest.mark.parametrize("interruption", [signal.SIGTERM, signal.SIGKILL])
def test_restart_worker_reclaims_after_interrupted_lookup(
    upgraded, monkeypatch, interruption
):
    async def check():
        async with parked_restart(upgraded, monkeypatch) as fixture:
            db, approvals, _, _, _, sender, send, lookup, intents, _, keys = fixture
            ident = sender.execution_id
            async with worker_login(db, refund=False, restart=True) as engine:
                program = """
import asyncio
from gateway import execution_worker as worker
async def hold(*args):
    print('holding_restart_lookup', flush=True)
    await asyncio.Event().wait()
worker.read_restart_evidence = hold
raise SystemExit(worker.main())
"""
                proc = await start_worker(
                    engine,
                    program=(program,),
                    drain_seconds=0.2,
                    restart_verification_keys=key_setting(keys),
                )
                try:
                    await event_from(proc, b"holding_restart_lookup")
                finally:
                    expected_exit = (
                        0 if interruption == signal.SIGTERM else -signal.SIGKILL
                    )
                    assert await stop_worker(proc, interruption) == expected_exit
                state, [original, abandoned] = await snapshot(db, ident)
                assert original == send and abandoned["outcome"] is None
                assert state["state"] == "UNKNOWN" and state["receipts"] == 0
                await expire_synthetic_lease(db, ident)
                store = SandboxRestartStore(
                    engine, approvals, timedelta(seconds=10), keys[-1]
                )
                assert await store.discover_due_synthetic_refunds() == [ident]
                contenders = await asyncio.gather(
                    store.claim_synthetic_refund_verification(ident, uuid4()),
                    store.claim_synthetic_refund_verification(ident, uuid4()),
                )
                [claim] = [value for value in contenders if value is not None]
                assert (
                    claim.sender == sender
                    and claim.lease.owner.generation == abandoned["generation"] + 1
                )
                stale = claim.model_copy(
                    update={
                        "lease": claim.lease.model_copy(
                            update={
                                "owner": claim.lease.owner.model_copy(
                                    update={
                                        "generation": abandoned["generation"],
                                        "worker_id": abandoned["worker_id"],
                                    }
                                ),
                            }
                        )
                    }
                )
                [intent] = intents
                evidence = await lookup(intent)
                assert evidence is not None
                assert (
                    await store.finish_synthetic_refund_verification(
                        stale,
                        evidence=evidence,
                        binding_preimage=intent.binding_preimage(),
                    )
                    is None
                )
                assert (
                    await store.finish_synthetic_refund_verification(
                        claim,
                        evidence=evidence,
                        binding_preimage=intent.binding_preimage(),
                    )
                    is ExecutionState.SUCCEEDED
                )
                await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
                assert await effects(db, ident) == (1, 1, 8)

    asyncio.run(check())


def test_restart_timeout_exhaustion_and_authorized_resume(upgraded, monkeypatch):
    async def check():
        async with parked_restart(upgraded, monkeypatch) as fixture:
            db, approvals, _, _, _, sender, send, lookup, _, _, keys = fixture
            ident = sender.execution_id
            async with worker_login(db, refund=False, restart=True) as engine:
                await configure(db, attempts=1, lookup=250)
                store = SandboxRestartStore(
                    engine, approvals, timedelta(seconds=20), keys[-1]
                )
                seen = []

                async def unavailable(intent):
                    seen.append(intent)
                    await asyncio.Event().wait()

                async with asyncio.timeout(5):
                    assert (
                        await RestartReconciler(approvals, store, unavailable).run_once(
                            ident, uuid4()
                        )
                        is ExecutionState.UNKNOWN
                    )
                assert len(seen) == 1 and seen[0].owner == sender
                state, [original, timed_out] = await snapshot(db, ident)
                assert original == send and timed_out["outcome"] == "UNKNOWN"
                assert state["status"] == "UNKNOWN" and state["receipts"] == 0
                assert state["command_row"]["next_check_at"] is None
                assert [event["action"] for event in state["audit_rows"][-2:]] == [
                    "SYNTHETIC_RESTART_VERIFY_DEFERRED_TIMEOUT",
                    "SYNTHETIC_RESTART_VERIFY_ATTENTION_ATTEMPTS",
                ]
                assert await store.discover_due_synthetic_refunds() == []
                assert (
                    await store.claim_synthetic_refund_verification(ident, uuid4())
                    is None
                )

                async with operator_login(db) as refund_operator:
                    assert await attention(refund_operator, ident, tool=TOOL) == []
                    with pytest.raises(DBAPIError) as denied:
                        await resume(refund_operator, ident, 1, tool=TOOL)
                    assert denied.value.orig.sqlstate == "42501"
                async with operator_login(db, restart=True) as operator:
                    [record] = await attention(operator, ident, tool=TOOL)
                    assert (
                        record["attempts_used"] == 1 and record["reason"] == "ATTEMPTS"
                    )
                    assert await resume(operator, ident, 1, tool=TOOL) == [
                        {"window_number": 2}
                    ]
                    assert await resume(operator, ident, 1, tool=TOOL) == []
                    resumed, history = await snapshot(db, ident)
                    assert history == [send, timed_out]
                    event = resumed["audit_rows"][-1]
                    assert event["actor"] == OPERATOR
                    assert event["action"].startswith(
                        "SYNTHETIC_RESTART_VERIFY_RESUMED:2:"
                    )

                assert (
                    await RestartReconciler(approvals, store, lookup).run_once(
                        ident, uuid4()
                    )
                    is ExecutionState.SUCCEEDED
                )
                await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
                assert await effects(db, ident) == (1, 1, 8)

    asyncio.run(check())


def test_restart_missing_evidence_and_elapsed_deadline(upgraded, monkeypatch):
    async def check():
        async with parked_restart(upgraded, monkeypatch, committed=False) as fixture:
            db, approvals, _, _, _, sender, send, lookup, _, _, keys = fixture
            ident = sender.execution_id
            async with worker_login(db, refund=False, restart=True) as engine:
                await configure(db, elapsed=30000, initial=60000, cap=60000)
                store = SandboxRestartStore(
                    engine, approvals, timedelta(seconds=20), keys[-1]
                )
                assert (
                    await RestartReconciler(approvals, store, lookup).run_once(
                        ident, uuid4()
                    )
                    is ExecutionState.UNKNOWN
                )
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
                        UPDATE execution_commands SET next_check_at=statement_timestamp()-interval '1 second'
                        WHERE execution_id=:id
                    """,
                        id=ident,
                    )
                assert await store.discover_due_synthetic_refunds() == [ident]
                assert (
                    await store.claim_synthetic_refund_verification(ident, uuid4())
                    is None
                )
                state, [original, attempt] = await snapshot(db, ident)
                assert original == send and attempt["outcome"] == "UNKNOWN"
                assert (
                    state["state"] == state["status"] == "UNKNOWN"
                    and state["receipts"] == 0
                )
                assert (
                    state["audit_rows"][-1]["action"]
                    == "SYNTHETIC_RESTART_VERIFY_ATTENTION_DEADLINE"
                )
                assert await store.discover_due_synthetic_refunds() == []
                assert await effects(db, ident) == (0, 0, 7)

    asyncio.run(check())


def test_send_refund_and_restart_make_progress_in_one_worker(upgraded, monkeypatch):
    async def check():
        async with parked_restart(upgraded, monkeypatch) as fixture:
            db, approvals, ownership, _, _, sender, send, _, _, refund, keys = fixture
            ident = sender.execution_id
            commands = services(db, keys)[0]
            refund_command = await commands.submit(
                replace(REQUEST, idempotency_key=str(uuid4()))
            )
            refund_id = refund_command.execution_id
            await approvals.decide(
                refund_id, "refund-approver", "APPROVED", "fixture refund"
            )
            async with db.begin() as conn:
                await execute(
                    conn,
                    "INSERT INTO synthetic_refund.captures VALUES (:workspace,:customer,:transaction,:currency,500)",
                    workspace=REQUEST.workspace_id,
                    customer=REQUEST.arguments["customer_id"],
                    transaction=REQUEST.arguments["transaction_id"],
                    currency=REQUEST.arguments["currency"],
                )
            refund_sender, refund_send, _, _ = await parked_send(
                db,
                ownership,
                refund,
                RefundFlow(approvals, ownership, refund),
                refund_id,
                monkeypatch,
                restart=False,
            )
            send_command = await commands.submit(
                replace(REQUEST, idempotency_key=str(uuid4()))
            )
            send_id = send_command.execution_id
            await approvals.decide(
                send_id, "refund-approver", "APPROVED", "fixture SEND"
            )
            async with worker_login(db, restart=True) as engine:
                worker = await start_worker(
                    engine,
                    refund_verification_keys=key_setting(keys),
                    restart_verification_keys=key_setting(keys),
                )
                try:
                    assert str(send_id).encode() in await event_from(
                        worker, b"ownership_claimed"
                    )
                    assert b"SUCCEEDED" in await event_from(worker, b"refund_verified")
                    assert b"SUCCEEDED" in await event_from(worker, b"restart_verified")
                    await event_from(worker, b"ownership_renewed")
                    held, [attempt] = await snapshot(db, send_id)
                    assert (
                        held["state"] == "CLAIMED" and attempt["send_marked_at"] is None
                    )
                    await check_terminal(
                        db, refund_sender, refund_send, ExecutionState.SUCCEEDED
                    )
                    await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
                    assert await effect(db, refund_id) == (1, 123, 377)
                    assert await effects(db, ident) == (1, 1, 8)
                finally:
                    assert await stop_worker(worker) == 0

    asyncio.run(check())


def test_restart_database_role_and_audit_mutation_are_scoped(upgraded, monkeypatch):
    async def check():
        async with parked_restart(upgraded, monkeypatch) as fixture:
            db, approvals, _, _, _, sender, _, lookup, _, _, keys = fixture
            ident = sender.execution_id
            async with (
                worker_login(db, refund=False, restart=True) as engine,
                audit_logins(db) as (app, inheritor),
            ):
                store = SandboxRestartStore(
                    engine, approvals, timedelta(seconds=20), keys[-1]
                )
                await store.validate_access()
                denied_statements = (
                    "DELETE FROM synthetic_restart.outcomes WHERE operation_id=:id",
                    "UPDATE synthetic_restart.services SET boot_generation=100",
                    "INSERT INTO synthetic_restart.arrivals VALUES (:id)",
                    "UPDATE execution_commands SET state='SUCCEEDED' WHERE execution_id=:id",
                    "DELETE FROM execution_attempts WHERE execution_id=:id",
                    "SELECT * FROM m914_refund.operate('claim',:id,gen_random_uuid(),NULL,1000,NULL,NULL,NULL,'payments.issue_refund')",
                    "SELECT * FROM synthetic_refund.outcomes",
                )
                for statement in denied_statements:
                    with pytest.raises(DBAPIError) as denied:
                        async with engine.begin() as conn:
                            await execute(conn, statement, id=ident)
                    assert denied.value.orig.sqlstate == "42501"

                assert (
                    await RestartReconciler(approvals, store, lookup).run_once(
                        ident, uuid4()
                    )
                    is ExecutionState.SUCCEEDED
                )
                state, _ = await snapshot(db, ident)
                [event] = [
                    row
                    for row in state["audit_rows"]
                    if row["action"] == "SYNTHETIC_RESTART_VERIFY_SUCCEEDED"
                ]
                for statement in (
                    "INSERT INTO public.audit_events(actor,action,resource_id) VALUES ('forged','SYNTHETIC_RESTART_SEND_PARKED',:id)",
                    "INSERT INTO public.audit_events(actor,action,resource_id) VALUES ('forged','SYNTHETIC_RESTART_VERIFY_RESUMED',:id)",
                    "INSERT INTO public.audit_events(actor,action,resource_id) VALUES ('forged','SYNTHETIC_RESTART_VERIFY_ATTENTION_DEADLINE',:id)",
                ):
                    with pytest.raises(DBAPIError) as denied:
                        async with app.begin() as conn:
                            await execute(conn, statement, id=ident)
                    assert denied.value.orig.sqlstate == "42501"
                for statement in (
                    "UPDATE public.audit_events SET actor='forged' WHERE id=:id",
                    "DELETE FROM public.audit_events WHERE id=:id",
                ):
                    with pytest.raises(DBAPIError) as denied:
                        async with inheritor.begin() as conn:
                            await execute(conn, statement, id=event["id"])
                    assert denied.value.orig.sqlstate == "42501"
                assert (await snapshot(db, ident))[0] == state
                assert await effects(db, ident) == (1, 1, 8)

    asyncio.run(check())


def test_absent_restart_lookup_can_resolve_on_later_committed_evidence(
    upgraded, monkeypatch
):
    async def check():
        async with parked_restart(upgraded, monkeypatch) as fixture:
            db, approvals, _, _, _, sender, send, lookup, _, _, keys = fixture
            ident = sender.execution_id
            async with worker_login(db, refund=False, restart=True) as engine:
                store = SandboxRestartStore(
                    engine, approvals, timedelta(seconds=20), keys[-1]
                )

                async def missing(_intent):
                    return None

                assert (
                    await RestartReconciler(approvals, store, missing).run_once(
                        ident, uuid4()
                    )
                    is ExecutionState.UNKNOWN
                )
                state, [original, attempt] = await snapshot(db, ident)
                assert original == send and attempt["outcome"] == "UNKNOWN"
                assert state["receipts"] == 0 and state["command_row"]["next_check_at"]
                assert (
                    state["audit_rows"][-1]["action"]
                    == "SYNTHETIC_RESTART_VERIFY_DEFERRED_NO_EVIDENCE"
                )
                await make_due(db, ident)
                assert (
                    await RestartReconciler(approvals, store, lookup).run_once(
                        ident, uuid4()
                    )
                    is ExecutionState.SUCCEEDED
                )
                await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
                assert await effects(db, ident) == (1, 1, 8)

    asyncio.run(check())


def test_invalid_restart_markers_do_not_block_later_work(upgraded, monkeypatch):
    async def check():
        async with parked_restart(upgraded, monkeypatch) as fixture:
            db, approvals, ownership, adapter, flow, oldest, _, _, _, _, keys = fixture
            commands = services(db, keys)[0]
            senders = [oldest]
            for _ in range(2):
                command = await commands.submit(
                    replace(
                        REQUEST,
                        idempotency_key=str(uuid4()),
                        requester="it-agent",
                        tool_id=TOOL,
                        arguments={
                            "customer_id": "C-102",
                            "service_id": "synthetic-search-api",
                        },
                    )
                )
                await approvals.decide(
                    command.execution_id,
                    "refund-approver",
                    "APPROVED",
                    "fixture restart",
                )
                sender, valid_send, _, _ = await parked_send(
                    db, ownership, adapter, flow, command.execution_id, monkeypatch
                )
                senders.append(sender)

            malformed, duplicated, valid = [sender.execution_id for sender in senders]
            async with db.begin() as conn:
                # Pin the initial queue order without sleeping between candidates.
                await execute(
                    conn,
                    """
                    UPDATE execution_commands SET next_check_at=statement_timestamp()
                        - CASE execution_id WHEN :malformed THEN interval '3 seconds'
                          WHEN :duplicated THEN interval '2 seconds' ELSE interval '1 second' END
                    WHERE execution_id IN (:malformed,:duplicated,:valid)
                """,
                    malformed=malformed,
                    duplicated=duplicated,
                    valid=valid,
                )

            async with worker_login(db, refund=False, restart=True) as engine:
                store = SandboxRestartStore(
                    engine, approvals, timedelta(seconds=20), keys[-1]
                )
                assert await asyncio.gather(
                    *(store.discover_due_synthetic_refunds(limit=2) for _ in range(2))
                ) == [[malformed, duplicated], [malformed, duplicated]]
                async with db.begin() as conn:
                    # Mutate between discovery and claim as a trusted fixture provisioner.
                    await execute(
                        conn,
                        """
                        ALTER TABLE public.audit_events DISABLE TRIGGER protect_synthetic_consumption_rows
                    """,
                    )
                    await execute(
                        conn,
                        """
                        UPDATE public.audit_events
                        SET action='SYNTHETIC_SEND_MARKED:it.restart_service:it-restart-ledger.v1:malformed'
                        WHERE resource_id=:id AND starts_with(action,'SYNTHETIC_SEND_MARKED:')
                    """,
                        id=malformed,
                    )
                    await execute(
                        conn,
                        """
                        ALTER TABLE public.audit_events ENABLE ALWAYS TRIGGER protect_synthetic_consumption_rows
                    """,
                    )
                    await execute(
                        conn,
                        """
                        INSERT INTO public.audit_events(actor,action,resource_id)
                        SELECT actor,action,resource_id FROM public.audit_events
                        WHERE resource_id=:id AND starts_with(action,'SYNTHETIC_SEND_MARKED:')
                    """,
                        id=duplicated,
                    )

                invalid_before = [
                    await snapshot(db, candidate)
                    for candidate in (malformed, duplicated)
                ]
                assert (
                    await asyncio.gather(
                        *(
                            store.claim_synthetic_refund_verification(
                                candidate, uuid4()
                            )
                            for candidate in (
                                malformed,
                                malformed,
                                duplicated,
                                duplicated,
                            )
                        )
                    )
                    == [None] * 4
                )
                assert await asyncio.gather(
                    *(store.discover_due_synthetic_refunds(limit=1) for _ in range(3))
                ) == [[valid], [valid], [valid]]

                workers = await asyncio.gather(
                    *(
                        start_worker(
                            engine, restart_verification_keys=key_setting(keys)
                        )
                        for _ in range(2)
                    )
                )
                try:
                    async with asyncio.timeout(10):
                        while (await snapshot(db, valid))[0]["state"] != "SUCCEEDED":
                            assert all(proc.returncode is None for proc in workers)
                            await asyncio.sleep(0.05)
                    await check_terminal(
                        db, senders[-1], valid_send, ExecutionState.SUCCEEDED
                    )
                    for _ in range(3):
                        assert await store.discover_due_synthetic_refunds(limit=1) == []
                    for candidate, original in zip(
                        (malformed, duplicated), invalid_before, strict=True
                    ):
                        state, [attempt] = await snapshot(db, candidate)
                        assert (state, [attempt]) == original
                        assert state["status"] == state["state"] == "UNKNOWN"
                        assert state["receipts"] == 0 and attempt["kind"] == "SEND"
                    async with db.connect() as conn:
                        versions = await rows(
                            conn,
                            """
                            SELECT operation_id,boot_generation FROM synthetic_restart.outcomes
                            WHERE operation_id IN (:malformed,:duplicated,:valid) ORDER BY boot_generation
                        """,
                            malformed=malformed,
                            duplicated=duplicated,
                            valid=valid,
                        )
                    assert versions == [
                        {"operation_id": malformed, "boot_generation": 8},
                        {"operation_id": duplicated, "boot_generation": 9},
                        {"operation_id": valid, "boot_generation": 10},
                    ]
                    for candidate in (malformed, duplicated, valid):
                        assert await effects(db, candidate) == (1, 1, 10)
                finally:
                    assert await asyncio.gather(
                        *(stop_worker(proc) for proc in workers)
                    ) == [0, 0]

    asyncio.run(check())
