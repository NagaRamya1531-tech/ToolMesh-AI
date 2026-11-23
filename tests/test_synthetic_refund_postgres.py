"""End-to-end refund sandbox tests against both PostgreSQL baselines."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from hmac import digest
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncTransaction
from synthetic_execution_adapter import SyntheticAdapter
from synthetic_refund_adapter import RefundIntent, RefundLedger, RefundRequest
from synthetic_refund_flow import RefundFlow
from test_durable_execution_schema import IMAGE, migrate
from test_durable_execution_schema import sandbox as sandbox
from test_execution_store import REQUEST, ledger
from test_execution_worker_postgres import expire_synthetic_lease, snapshot

from gateway.execution_approval import ExecutionApprovals
from gateway.execution_contracts import ExecutionOwner, ExecutionState
from gateway.execution_crypto import (
    CommandDigests,
    PayloadProtection,
    SandboxWrappingKeys,
)
from gateway.execution_store import CommandStore, CommandStoreError
from gateway.execution_worker_store import (
    ExecutionWorkerStore,
    synthetic_evidence_bytes,
)
from shared.database import execute, rows

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires disposable PostgreSQL image"
)


@pytest.fixture(scope="module")
def upgraded(sandbox):
    _, env, _ = sandbox
    migrate(env, "upgrade", "0011")
    return sandbox


def services(db, keys):
    wrapping, lookup, intent, evidence = keys
    payloads = PayloadProtection(SandboxWrappingKeys({1: wrapping}, 1))
    digests = CommandDigests(lookup, intent)
    approvals = ExecutionApprovals(db, payloads, digests, timedelta(minutes=3))
    ownership = ExecutionWorkerStore(db, timedelta(seconds=30), synthetic_key=evidence)
    downstream = RefundLedger(db, evidence)
    return CommandStore(db, payloads, digests), approvals, ownership, downstream


@asynccontextmanager
async def scenario(upgraded, *, seed=True):
    async with ledger(upgraded) as (db, _, _, _):
        keys = [os.urandom(32) for _ in range(4)]
        commands, approvals, ownership, downstream = services(db, keys)
        guards = SyntheticAdapter(db, keys[-1])
        await guards.initialize()
        await downstream.initialize()
        try:
            request = replace(REQUEST, idempotency_key=str(uuid4()))
            command = await commands.submit(request)
            duplicate = await commands.submit(request)
            assert duplicate.execution_id == command.execution_id
            ident = command.execution_id
            await approvals.decide(
                ident, "refund-approver", "APPROVED", "fixture refund"
            )
            if seed:
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        INSERT INTO synthetic_refund.captures VALUES (:workspace, :customer, :transaction, :currency, 500)
                        """,
                        workspace=request.workspace_id,
                        customer=request.arguments["customer_id"],
                        transaction=request.arguments["transaction_id"],
                        currency=request.arguments["currency"],
                    )
            yield (
                db,
                ownership,
                downstream,
                RefundFlow(approvals, ownership, downstream),
                ident,
                keys,
            )
        finally:
            await downstream.close()
            await guards.close()
            async with db.begin() as conn:
                for table in (
                    "execution_receipts",
                    "execution_attempts",
                    "execution_approval_decisions",
                ):
                    await execute(conn, f"DELETE FROM {table}")


async def effect(db, ident):
    async with db.connect() as conn:
        [record] = await rows(
            conn,
            """
            SELECT (SELECT count(*) FROM synthetic_refund.arrivals WHERE operation_id=:id) AS arrivals,
                   (SELECT refunded_minor FROM synthetic_refund.outcomes WHERE operation_id=:id) AS refunded,
                   (SELECT available_minor FROM synthetic_refund.captures) AS remaining
            """,
            id=ident,
        )
    return tuple(record.values())


async def current_owner(db, ident):
    _, [attempt] = await snapshot(db, ident)
    return ExecutionOwner(
        execution_id=ident,
        generation=attempt["generation"],
        worker_id=attempt["worker_id"],
    )


@pytest.mark.parametrize("seed", [True, False])
def test_refund_terminal_evidence(upgraded, seed):
    async def check():
        async with scenario(upgraded, seed=seed) as (db, _, _, flow, ident, _):
            expected = ExecutionState.SUCCEEDED if seed else ExecutionState.FAILED
            assert await flow.run_once(uuid4()) is expected
            state, [attempt] = await snapshot(db, ident)
            assert state["state"] == state["status"] == expected
            assert state["receipts"] == 1 and attempt["send_marked_at"]
            assert state["receipt_row"]["evidence_source"] == "LOOKUP"
            assert await effect(db, ident) == ((1, 123, 377) if seed else (1, 0, None))
            assert await flow.recover(await current_owner(db, ident)) is expected
            assert await flow.run_once(uuid4()) is None
            assert await snapshot(db, ident) == (state, [attempt])

    asyncio.run(check())


@pytest.mark.parametrize(
    "fault",
    [
        "consume-ack",
        "consume-rollback",
        "effect-ack",
        "effect-rollback",
        "finalize-ack",
        "finalize-rollback",
    ],
)
def test_refund_transaction_ambiguity(upgraded, monkeypatch, fault):
    async def check():
        async with scenario(upgraded) as (db, ownership, downstream, flow, ident, _):
            phase, kind = fault.split("-")
            target, name, commit_number = {
                "consume": (ownership, "consume_synthetic_send", 1),
                "effect": (downstream, "invoke", 2),
                "finalize": (ownership, "finalize_synthetic", 1),
            }[phase]
            original = getattr(target, name)
            exit_tx = AsyncTransaction.__aexit__

            async def inject(*args, **kwargs):
                calls = 0

                async def fail_ack(tx, *failure):
                    nonlocal calls
                    calls += 1
                    if calls != commit_number:
                        await exit_tx(tx, *failure)
                        return
                    assert failure[0] is None
                    if kind == "ack":
                        await exit_tx(tx, *failure)
                    else:
                        await tx.rollback()
                    raise OSError("fixture transaction fault")

                with monkeypatch.context() as patch:
                    patch.setattr(AsyncTransaction, "__aexit__", fail_ack)
                    return await original(*args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(target, name, inject)
                if phase == "consume":
                    with pytest.raises(
                        CommandStoreError, match="^EXECUTION_OWNERSHIP_UNAVAILABLE$"
                    ):
                        await flow.run_once(uuid4())
                else:
                    expected = (
                        ExecutionState.UNKNOWN
                        if kind == "rollback"
                        else ExecutionState.SUCCEEDED
                    )
                    initial = (
                        ExecutionState.DISPATCHING
                        if fault == "finalize-rollback"
                        else expected
                    )
                    assert await flow.run_once(uuid4()) is initial

            owner = await current_owner(db, ident)
            if phase == "consume":
                assert await effect(db, ident) == (0, None, 500)
                await expire_synthetic_lease(db, ident)
                expected = ExecutionState.UNKNOWN
            if fault == "finalize-rollback":
                assert (await snapshot(db, ident))[0]["receipts"] == 0
                expected = ExecutionState.SUCCEEDED
            assert await flow.recover(owner) is expected
            state, [attempt] = await snapshot(db, ident)
            assert state["receipts"] == int(expected is ExecutionState.SUCCEEDED)
            assert attempt["outcome"] == expected
            expected_effect = {
                "consume": (0, None, 500),
                "effect": (1, 123, 377) if kind == "ack" else (1, None, 500),
                "finalize": (1, 123, 377),
            }
            assert await effect(db, ident) == expected_effect[phase]
            assert await flow.run_once(uuid4()) is None

    asyncio.run(check())


@pytest.mark.parametrize("fault", ["mismatch", "expired"])
def test_refund_rejects_invalid_admission_or_evidence(upgraded, monkeypatch, fault):
    async def check():
        async with scenario(upgraded) as (db, ownership, _, flow, ident, _):

            async def after_consumption():
                if fault == "mismatch":
                    async with db.begin() as conn:
                        await execute(
                            conn,
                            "INSERT INTO synthetic_refund.outcomes VALUES (:id,:binding,0)",
                            id=ident,
                            binding=bytes(32),
                        )
                else:
                    await expire_synthetic_lease(db, ident)

            assert (
                await flow.run_once(uuid4(), after_consumption=after_consumption)
                is ExecutionState.UNKNOWN
            )
            assert await effect(db, ident) == (
                (1, 0, 500) if fault == "mismatch" else (1, 123, 377)
            )
            owner = await current_owner(db, ident)
            assert await flow.recover(owner) is ExecutionState.UNKNOWN
            state, _ = await snapshot(db, ident)
            assert state["receipts"] == 0
            assert await flow.run_once(uuid4()) is None

    asyncio.run(check())


_CHILD = """
import asyncio, json, os, sys
from uuid import uuid4
from sqlalchemy.ext.asyncio import create_async_engine
from test_synthetic_refund_postgres import services
from synthetic_refund_flow import RefundFlow

async def main():
    config = json.loads(await asyncio.to_thread(sys.stdin.readline))
    db = create_async_engine(config['url'], hide_parameters=True)
    try:
        _, approvals, ownership, downstream = services(db, [bytes.fromhex(v) for v in config['keys']])
        async def after_consumption():
            if config['crash']:
                os._exit(17)
        print('ready', flush=True)
        assert await asyncio.to_thread(sys.stdin.readline) == 'start\\n'
        result = await RefundFlow(approvals, ownership, downstream).run_once(
            uuid4(), after_consumption=after_consumption
        )
        print(result, flush=True)
    finally:
        await db.dispose()

asyncio.run(main())
"""


@pytest.mark.parametrize("crash", [False, True])
def test_refund_races_independent_processes(upgraded, crash):
    async def check():
        async with scenario(upgraded) as (db, _, _, flow, ident, keys):
            config = (
                json.dumps(
                    {
                        "url": db.url.render_as_string(hide_password=False),
                        "keys": [key.hex() for key in keys],
                        "crash": crash,
                    }
                ).encode()
                + b"\n"
            )
            processes = []
            try:
                for _ in range(2):
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-c",
                        _CHILD,
                        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent)},
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    processes.append(process)
                    process.stdin.write(config)
                    await process.stdin.drain()
                    assert (
                        await asyncio.wait_for(process.stdout.readline(), 10)
                        == b"ready\n"
                    )
                assert len({process.pid for process in processes}) == 2
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *(process.communicate(b"start\n") for process in processes)
                    ),
                    10,
                )
                expected_exits = [0, 17] if crash else [0, 0]
                assert (
                    sorted(process.returncode for process in processes)
                    == expected_exits
                ), results
                assert sorted(output.strip() for output, _ in results) == (
                    [b"", b"None"] if crash else [b"None", b"SUCCEEDED"]
                )
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
            owner = await current_owner(db, ident)
            if crash:
                await expire_synthetic_lease(db, ident)
            expected = ExecutionState.UNKNOWN if crash else ExecutionState.SUCCEEDED
            assert await flow.recover(owner) is expected
            assert await effect(db, ident) == (
                (0, None, 500) if crash else (1, 123, 377)
            )
            assert await flow.run_once(uuid4()) is None

    asyncio.run(check())


def test_refund_conflicting_terminal_writes_and_stale_recovery(upgraded, monkeypatch):
    async def check():
        async with scenario(upgraded) as (db, ownership, _, flow, ident, _):
            original = ownership.finalize_synthetic
            competing_writes = []

            async def race(evidence):
                competing_writes.extend(
                    await asyncio.gather(
                        original(evidence),
                        ownership.classify_synthetic_unknown(evidence.owner),
                    )
                )
                return competing_writes[0]

            with monkeypatch.context() as patch:
                patch.setattr(ownership, "finalize_synthetic", race)
                result = await flow.run_once(uuid4())
            assert competing_writes.count(True) == 1
            state, [attempt] = await snapshot(db, ident)
            assert result in {ExecutionState.SUCCEEDED, ExecutionState.UNKNOWN}
            assert state["receipts"] == int(result is ExecutionState.SUCCEEDED)
            assert state["state"] == attempt["outcome"] == result
            assert await effect(db, ident) == (1, 123, 377)
            owner = await current_owner(db, ident)
            stale = owner.model_copy(update={"generation": owner.generation + 1})
            assert await flow.recover(stale) is None
            assert await flow.recover(owner) is result
            assert await snapshot(db, ident) == (state, [attempt])

    asyncio.run(check())


def test_refund_ledger_deduplicates_downstream_arrivals(upgraded):
    async def check():
        async with scenario(upgraded) as (db, _, downstream, flow, ident, keys):
            _, approvals, _, _ = services(db, keys)

            async def inject_duplicate_arrivals():
                owner = await current_owner(db, ident)
                (
                    scope,
                    arguments,
                    sealed_digest,
                ) = await approvals.read_sealed_dispatch_intent(
                    ident, "payments.issue_refund"
                )
                intent = RefundIntent(
                    owner, scope, RefundRequest.model_validate(arguments), sealed_digest
                )
                await asyncio.gather(
                    downstream.invoke(intent), downstream.invoke(intent)
                )
                conflicting = replace(
                    intent,
                    request=intent.request.model_copy(update={"amount_minor": 200}),
                )
                await downstream.invoke(conflicting)
                assert await downstream.evidence(conflicting) is None

            assert (
                await flow.run_once(
                    uuid4(), after_consumption=inject_duplicate_arrivals
                )
                is ExecutionState.SUCCEEDED
            )
            assert await effect(db, ident) == (4, 123, 377)
            state, [attempt] = await snapshot(db, ident)
            assert state["receipts"] == 1 and attempt["outcome"] == "SUCCEEDED"

    asyncio.run(check())


@pytest.mark.parametrize(
    "mutation, error",
    [
        ("requester", "REQUESTER_AUTHORIZATION_CHANGED"),
        ("approver", "APPROVER_AUTHORIZATION_CHANGED"),
        ("catalog", "REQUESTER_AUTHORIZATION_CHANGED"),
        ("ciphertext", "EXECUTION_PAYLOAD_UNAVAILABLE"),
    ],
)
def test_refund_revalidates_before_consumption(upgraded, monkeypatch, mutation, error):
    async def check():
        async with scenario(upgraded) as (db, ownership, _, flow, ident, _):
            original = ownership.consume_synthetic_send
            sql, undo = {
                "requester": (
                    "UPDATE refund_grants SET enabled=false WHERE principal='finance-agent'",
                    "UPDATE refund_grants SET enabled=true WHERE principal='finance-agent'",
                ),
                "approver": (
                    "UPDATE execution_approval_entitlements SET enabled=false WHERE principal='refund-approver' AND tool_id='payments.issue_refund'",
                    "UPDATE execution_approval_entitlements SET enabled=true WHERE principal='refund-approver' AND tool_id='payments.issue_refund'",
                ),
                "catalog": (
                    "UPDATE tools SET lifecycle='quarantined' WHERE id='payments.issue_refund'",
                    "UPDATE tools SET lifecycle='published' WHERE id='payments.issue_refund'",
                ),
                "ciphertext": (
                    "UPDATE execution_commands SET ciphertext=set_byte(ciphertext,0,get_byte(ciphertext,0) # 1) WHERE execution_id=:id",
                )
                * 2,
            }[mutation]
            held_marker = None

            async def change_after_mark(marker, **kwargs):
                nonlocal held_marker
                held_marker = marker
                async with db.begin() as conn:
                    await execute(conn, sql, id=ident)
                return await original(marker, **kwargs)

            try:
                with monkeypatch.context() as patch:
                    patch.setattr(
                        ownership, "consume_synthetic_send", change_after_mark
                    )
                    with pytest.raises(CommandStoreError, match=f"^{error}$"):
                        await flow.run_once(uuid4())
                assert held_marker is not None
                state, [attempt] = await snapshot(db, ident)
                assert state["state"] == "DISPATCHING" and attempt["send_marked_at"]
                assert state["receipts"] == 0 and attempt["outcome"] is None
                assert not any(
                    event["action"] == "SYNTHETIC_INVOCATION_CONSUMED"
                    for event in state["audit_rows"]
                )
                with pytest.raises(
                    CommandStoreError, match="^SYNTHETIC_EVIDENCE_MISMATCH$"
                ):
                    await original(held_marker)
                assert await flow.run_once(uuid4()) is None
                assert await effect(db, ident) == (0, None, 500)
            finally:
                async with db.begin() as conn:
                    await execute(conn, undo, id=ident)
            owner = await current_owner(db, ident)
            await expire_synthetic_lease(db, ident)
            assert await flow.recover(owner) is ExecutionState.UNKNOWN
            assert await flow.run_once(uuid4()) is None
            assert await effect(db, ident) == (0, None, 500)

    asyncio.run(check())


def test_shared_finalizer_rejects_evidence_outside_refund_contract(
    upgraded, monkeypatch
):
    async def check():
        async with scenario(upgraded) as (db, ownership, _, flow, ident, keys):
            mark = ownership.mark_synthetic_send
            marker = None

            async def remember(*args, **kwargs):
                nonlocal marker
                marker = await mark(*args, **kwargs)
                return marker

            async def wrong_evidence():
                assert marker is not None
                generic = await SyntheticAdapter(db, keys[-1]).invoke(
                    marker, effect=True
                )
                wrong_binding = generic.model_copy(update={"intent_binding": bytes(32)})
                wrong_binding = wrong_binding.model_copy(
                    update={
                        "attestation": digest(
                            keys[-1], synthetic_evidence_bytes(wrong_binding), "sha256"
                        )
                    }
                )
                before = await snapshot(db, ident)
                for evidence in (generic, wrong_binding):
                    with pytest.raises(
                        CommandStoreError, match="^SYNTHETIC_EVIDENCE_MISMATCH$"
                    ):
                        await ownership.finalize_synthetic(evidence)
                    assert await snapshot(db, ident) == before
                assert before[0]["receipts"] == 0
                assert await effect(db, ident) == (0, None, 500)

            with monkeypatch.context() as patch:
                patch.setattr(ownership, "mark_synthetic_send", remember)
                assert (
                    await flow.run_once(uuid4(), after_consumption=wrong_evidence)
                    is ExecutionState.SUCCEEDED
                )
            state, _ = await snapshot(db, ident)
            assert (
                state["receipts"] == 1
                and state["receipt_row"]["evidence_source"] == "LOOKUP"
            )
            assert await effect(db, ident) == (1, 123, 377)

    asyncio.run(check())


def test_refund_expired_recovery_without_wrapping_key(upgraded):
    async def check():
        async with scenario(upgraded) as (db, _, _, flow, ident, keys):

            async def interrupt():
                raise RuntimeError("fixture interruption")

            with pytest.raises(RuntimeError, match="fixture interruption"):
                await flow.run_once(uuid4(), after_consumption=interrupt)
            owner = await current_owner(db, ident)
            _, approvals, ownership, downstream = services(
                db, [os.urandom(32), *keys[1:]]
            )
            recovery = RefundFlow(approvals, ownership, downstream)
            before = await snapshot(db, ident)
            with pytest.raises(
                CommandStoreError, match="^EXECUTION_PAYLOAD_UNAVAILABLE$"
            ):
                await recovery.recover(owner)
            assert await snapshot(db, ident) == before
            await expire_synthetic_lease(db, ident)
            assert await asyncio.gather(
                recovery.recover(owner), recovery.recover(owner)
            ) == [ExecutionState.UNKNOWN, ExecutionState.UNKNOWN]
            state, [attempt] = await snapshot(db, ident)
            assert state["state"] == attempt["outcome"] == "UNKNOWN"
            assert state["receipts"] == 0 and state["lease_owner"] is None
            assert (
                sum(
                    event["action"] == "SYNTHETIC_UNKNOWN"
                    for event in state["audit_rows"]
                )
                == 1
            )
            assert await recovery.run_once(uuid4()) is None
            assert await effect(db, ident) == (0, None, 500)

    asyncio.run(check())


def test_refund_ambiguous_marker_contract_fails_closed(upgraded, monkeypatch):
    async def check():
        async with scenario(upgraded) as (db, ownership, _, flow, ident, _):
            consume = ownership.consume_synthetic_send

            async def add_conflicting_contract(marker, **kwargs):
                async with db.begin() as conn:
                    await execute(
                        conn,
                        "INSERT INTO audit_events(actor, action, resource_id) VALUES (:actor,:action,:id)",
                        actor=str(marker.owner.worker_id),
                        action="SYNTHETIC_SEND_MARKED:payments.issue_refund:refund-ledger.v99:"
                        + "0" * 64,
                        id=ident,
                    )
                return await consume(marker, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(
                    ownership, "consume_synthetic_send", add_conflicting_contract
                )
                with pytest.raises(
                    CommandStoreError, match="^SYNTHETIC_EVIDENCE_MISMATCH$"
                ):
                    await flow.run_once(uuid4())
            state, [attempt] = await snapshot(db, ident)
            assert state["state"] == "DISPATCHING" and attempt["outcome"] is None
            assert state["receipts"] == 0
            assert not any(
                event["action"] == "SYNTHETIC_INVOCATION_CONSUMED"
                for event in state["audit_rows"]
            )
            assert await effect(db, ident) == (0, None, 500)

    asyncio.run(check())
