"""Synthetic IT restart lifecycle on fresh and upgraded disposable PostgreSQL."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from hmac import digest
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncTransaction
from synthetic_execution_adapter import SyntheticAdapter
from synthetic_it_restart import TOOL, RestartFlow, RestartLedger, restart_intent
from test_durable_execution_schema import IMAGE
from test_durable_execution_schema import sandbox as sandbox
from test_execution_store import REQUEST, ledger
from test_execution_worker_postgres import expire_synthetic_lease, snapshot
from test_execution_worker_postgres import upgraded as upgraded
from test_synthetic_refund_postgres import current_owner, services

from gateway.execution_contracts import ExecutionState
from gateway.execution_store import CommandStoreError
from gateway.execution_worker_store import synthetic_evidence_bytes
from shared.database import execute, rows

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires disposable PostgreSQL image"
)


@asynccontextmanager
async def restart_case(upgraded, *, allowed=True):
    async with ledger(upgraded) as (db, _, _, _):
        keys = [os.urandom(32) for _ in range(4)]
        commands, approvals, store, _ = services(db, keys)
        guards = SyntheticAdapter(db, keys[-1])
        adapter = RestartLedger(db, keys[-1])
        await guards.initialize()
        await adapter.initialize()
        try:
            request = replace(
                REQUEST,
                idempotency_key=str(uuid4()),
                requester="it-agent",
                tool_id=TOOL,
                arguments={
                    "customer_id": "C-102",
                    "service_id": "synthetic-search-api",
                },
            )
            async with db.begin() as conn:
                await execute(
                    conn,
                    "INSERT INTO synthetic_restart.services VALUES (:workspace,'C-102','synthetic-search-api',:allowed,7)",
                    workspace=request.workspace_id,
                    allowed=allowed,
                )
            admitted = await commands.submit(request)
            assert (
                await commands.submit(request)
            ).execution_id == admitted.execution_id
            assert (
                await approvals.decide(
                    admitted.execution_id,
                    "refund-approver",
                    "APPROVED",
                    "sandbox restart",
                )
            ).execution_state is ExecutionState.READY
            yield (
                db,
                approvals,
                store,
                adapter,
                RestartFlow(approvals, store, adapter),
                admitted.execution_id,
                keys,
            )
        finally:
            await adapter.close()
            await guards.close()
            async with db.begin() as conn:
                for table in (
                    "execution_receipts",
                    "execution_attempts",
                    "execution_approval_decisions",
                ):
                    await execute(conn, f"DELETE FROM {table}")


async def effects(db, ident):
    async with db.connect() as conn:
        [result] = await rows(
            conn,
            """
            SELECT (SELECT count(*) FROM synthetic_restart.arrivals WHERE operation_id=:id) AS arrivals,
                   (SELECT count(*) FROM synthetic_restart.outcomes WHERE operation_id=:id AND boot_generation IS NOT NULL) AS effects,
                   (SELECT boot_generation FROM synthetic_restart.services) AS service_generation
            """,
            id=ident,
        )
        return tuple(result.values())


async def get_intent(db, approvals, ident):
    owner = await current_owner(db, ident)
    scope, args, sealed = await approvals.read_sealed_dispatch_intent(ident, TOOL)
    return restart_intent(owner, scope, args, sealed)


@pytest.mark.parametrize("allowed", [True, False])
def test_restart_terminal_lookup(upgraded, allowed):
    async def check():
        async with restart_case(upgraded, allowed=allowed) as (
            db,
            approvals,
            _,
            adapter,
            flow,
            ident,
            _,
        ):
            expected = ExecutionState.SUCCEEDED if allowed else ExecutionState.FAILED
            assert await flow.run_once(uuid4()) is expected
            state, [attempt] = await snapshot(db, ident)
            assert state["state"] == attempt["outcome"] == expected
            assert (
                state["receipts"] == 1
                and state["receipt_row"]["evidence_source"] == "LOOKUP"
            )
            intent = await get_intent(db, approvals, ident)
            evidence = await adapter.evidence(intent)
            assert evidence and evidence.effect_version == (8 if allowed else None)
            assert await effects(db, ident) == (1, int(allowed), 8 if allowed else 7)
            assert await flow.recover(intent.owner) is expected
            assert await flow.run_once(uuid4()) is None
            await adapter.invoke(intent)
            assert await effects(db, ident) == (2, int(allowed), 8 if allowed else 7)
            assert await snapshot(db, ident) == (state, [attempt])

    asyncio.run(check())


def test_restart_independent_deduplication(upgraded):
    async def check():
        async with restart_case(upgraded) as (
            db,
            approvals,
            _,
            adapter,
            flow,
            ident,
            _,
        ):

            async def duplicate_fixture_arrivals():
                intent = await get_intent(db, approvals, ident)
                await asyncio.gather(adapter.invoke(intent), adapter.invoke(intent))
                wrong_scope = replace(
                    intent,
                    scope=intent.scope.model_copy(update={"customer_id": "C-404"}),
                )
                with pytest.raises(
                    CommandStoreError, match="^SYNTHETIC_RESTART_NOT_AUTHORIZED$"
                ):
                    await adapter.invoke(wrong_scope)
                assert await adapter.evidence(wrong_scope) is None

            assert (
                await flow.run_once(
                    uuid4(), after_consumption=duplicate_fixture_arrivals
                )
                is ExecutionState.SUCCEEDED
            )
            assert await effects(db, ident) == (4, 1, 8)
            assert (await snapshot(db, ident))[0]["receipts"] == 1

    asyncio.run(check())


@pytest.mark.parametrize(
    "fault",
    [
        "mark-ack",
        "consume-ack",
        "effect-ack",
        "effect-rollback",
        "finalize-ack",
        "finalize-rollback",
    ],
)
def test_restart_lost_ack_and_rollback(upgraded, monkeypatch, fault):
    async def check():
        async with restart_case(upgraded) as (db, _, store, adapter, flow, ident, _):
            phase, failure_kind = fault.split("-")
            target, method, transaction = {
                "mark": (store, "mark_synthetic_send", 1),
                "consume": (store, "consume_synthetic_send", 1),
                "effect": (adapter, "invoke", 2),
                "finalize": (store, "finalize_synthetic", 1),
            }[phase]
            original = getattr(target, method)
            commit = AsyncTransaction.__aexit__

            async def inject(*args, **kwargs):
                calls = 0

                async def exit_with_fault(tx, *failure):
                    nonlocal calls
                    calls += 1
                    if calls != transaction:
                        return await commit(tx, *failure)
                    assert failure[0] is None
                    if failure_kind == "ack":
                        await commit(tx, *failure)
                    else:
                        await tx.rollback()
                    raise OSError("fixture transaction failure")

                with monkeypatch.context() as patch:
                    patch.setattr(AsyncTransaction, "__aexit__", exit_with_fault)
                    return await original(*args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(target, method, inject)
                if phase in ("mark", "consume"):
                    with pytest.raises(
                        CommandStoreError, match="^EXECUTION_OWNERSHIP_UNAVAILABLE$"
                    ):
                        await flow.run_once(uuid4())
                else:
                    initial = {
                        "effect-ack": ExecutionState.SUCCEEDED,
                        "effect-rollback": ExecutionState.UNKNOWN,
                        "finalize-ack": ExecutionState.SUCCEEDED,
                        "finalize-rollback": ExecutionState.DISPATCHING,
                    }[fault]
                    assert await flow.run_once(uuid4()) is initial
            owner = await current_owner(db, ident)
            if phase in ("mark", "consume"):
                assert await effects(db, ident) == (0, 0, 7)
                await expire_synthetic_lease(db, ident)
            expected = (
                ExecutionState.UNKNOWN
                if phase in ("mark", "consume") or fault == "effect-rollback"
                else ExecutionState.SUCCEEDED
            )
            assert await flow.recover(owner) is expected
            actual = await effects(db, ident)
            assert actual == (
                (0, 0, 7)
                if phase in ("mark", "consume")
                else (1, 0, 7)
                if fault == "effect-rollback"
                else (1, 1, 8)
            )
            assert (await snapshot(db, ident))[0]["receipts"] == int(
                expected is ExecutionState.SUCCEEDED
            )
            assert await flow.run_once(uuid4()) is None
            assert await effects(db, ident) == actual

    asyncio.run(check())


def test_restart_rejects_refund_generic_and_wrong_scope_evidence(upgraded, monkeypatch):
    async def check():
        async with restart_case(upgraded) as (
            db,
            approvals,
            store,
            adapter,
            flow,
            ident,
            keys,
        ):
            original = store.finalize_synthetic

            async def contest(genuine):
                # The verifier supplied genuine restart evidence. Even a trusted
                # fixture signer cannot substitute a different marker contract.
                intent = await get_intent(db, approvals, ident)
                wrong_scope = replace(
                    intent,
                    scope=intent.scope.model_copy(update={"customer_id": "C-404"}),
                )
                alternatives = (
                    {
                        "intent_binding": None,
                        "evidence_contract": None,
                        "effect_version": None,
                    },
                    {"evidence_contract": None, "effect_version": None},
                    {"intent_binding": wrong_scope.binding},
                    {"operation_id": uuid4()},
                )
                before = await snapshot(db, ident)
                for update in alternatives:
                    evidence = genuine.model_copy(update=update)
                    evidence = evidence.model_copy(
                        update={
                            "attestation": digest(
                                keys[-1], synthetic_evidence_bytes(evidence), "sha256"
                            )
                        }
                    )
                    with pytest.raises(
                        CommandStoreError, match="^SYNTHETIC_EVIDENCE_MISMATCH$"
                    ):
                        await original(evidence)
                    assert await snapshot(db, ident) == before
                for update in (
                    {"evidence_contract": "it-restart-ledger.v99"},
                    {"effect_version": None},
                ):
                    with pytest.raises(
                        CommandStoreError, match="^SYNTHETIC_EVIDENCE_MISMATCH$"
                    ):
                        await original(genuine.model_copy(update=update))
                    assert await snapshot(db, ident) == before
                stale = genuine.model_copy(
                    update={
                        "owner": genuine.owner.model_copy(
                            update={"generation": genuine.owner.generation + 1}
                        )
                    }
                )
                stale = stale.model_copy(
                    update={
                        "attestation": digest(
                            keys[-1], synthetic_evidence_bytes(stale), "sha256"
                        )
                    }
                )
                assert not await original(stale)
                assert await adapter.evidence(intent) == genuine
                results = await asyncio.gather(
                    original(genuine), store.classify_synthetic_unknown(genuine.owner)
                )
                assert results.count(True) == 1
                return results[0]

            with monkeypatch.context() as patch:
                patch.setattr(store, "finalize_synthetic", contest)
                outcome = await flow.run_once(uuid4())
            assert outcome in (ExecutionState.SUCCEEDED, ExecutionState.UNKNOWN)
            assert (await snapshot(db, ident))[0]["receipts"] == int(
                outcome is ExecutionState.SUCCEEDED
            )
            assert await effects(db, ident) == (1, 1, 8)

    asyncio.run(check())


@pytest.mark.parametrize("changed", ["requester", "approver", "catalog", "sealed"])
def test_restart_rejects_changed_intent_or_authorization(
    upgraded, monkeypatch, changed
):
    async def check():
        async with restart_case(upgraded) as (db, _, store, _, flow, ident, _):
            original = store.consume_synthetic_send
            change, restore, error = {
                "requester": (
                    "UPDATE agent_accounts SET enabled=false WHERE id='it-agent'",
                    "UPDATE agent_accounts SET enabled=true WHERE id='it-agent'",
                    "REQUESTER_AUTHORIZATION_CHANGED",
                ),
                "approver": (
                    "UPDATE execution_approval_entitlements SET enabled=false WHERE principal='refund-approver' AND tool_id='it.restart_service'",
                    "UPDATE execution_approval_entitlements SET enabled=true WHERE principal='refund-approver' AND tool_id='it.restart_service'",
                    "APPROVER_AUTHORIZATION_CHANGED",
                ),
                "catalog": (
                    "UPDATE tools SET lifecycle='quarantined' WHERE id='it.restart_service'",
                    "UPDATE tools SET lifecycle='published' WHERE id='it.restart_service'",
                    "REQUESTER_AUTHORIZATION_CHANGED",
                ),
                "sealed": (
                    "UPDATE execution_commands SET ciphertext=set_byte(ciphertext,0,get_byte(ciphertext,0) # 1) WHERE execution_id=:id",
                )
                * 2
                + ("EXECUTION_PAYLOAD_UNAVAILABLE",),
            }[changed]

            async def revoke(marker, **kwargs):
                async with db.begin() as conn:
                    await execute(conn, change, id=ident)
                return await original(marker, **kwargs)

            try:
                with monkeypatch.context() as patch:
                    patch.setattr(store, "consume_synthetic_send", revoke)
                    with pytest.raises(CommandStoreError, match=f"^{error}$"):
                        await flow.run_once(uuid4())
                state, _ = await snapshot(db, ident)
                assert state["receipts"] == 0 and not any(
                    row["action"] == "SYNTHETIC_INVOCATION_CONSUMED"
                    for row in state["audit_rows"]
                )
                assert await effects(db, ident) == (0, 0, 7)
            finally:
                async with db.begin() as conn:
                    await execute(conn, restore, id=ident)
            await expire_synthetic_lease(db, ident)
            assert (
                await flow.recover(await current_owner(db, ident))
                is ExecutionState.UNKNOWN
            )
            assert await flow.run_once(uuid4()) is None
            assert await effects(db, ident) == (0, 0, 7)

    asyncio.run(check())


_CHILD = """
import asyncio, json, os, sys
from uuid import uuid4
from sqlalchemy.ext.asyncio import create_async_engine
from synthetic_it_restart import RestartFlow, RestartLedger
from test_synthetic_refund_postgres import services

async def main():
    config = json.loads(await asyncio.to_thread(sys.stdin.readline))
    db = create_async_engine(config["url"], hide_parameters=True)
    try:
        keys = [bytes.fromhex(key) for key in config["keys"]]
        _, approvals, store, _ = services(db, keys)
        async def crash():
            if config["crash"]:
                os._exit(17)
        print("ready", flush=True)
        assert await asyncio.to_thread(sys.stdin.readline) == "start\\n"
        print(await RestartFlow(approvals, store, RestartLedger(db, keys[-1])).run_once(uuid4(), after_consumption=crash), flush=True)
    finally:
        await db.dispose()
asyncio.run(main())
"""


@pytest.mark.parametrize("crash", [False, True])
def test_restart_independent_processes(upgraded, crash):
    async def check():
        async with restart_case(upgraded) as (db, _, _, _, flow, ident, keys):
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
                    child = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-c",
                        _CHILD,
                        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent)},
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    processes.append(child)
                    child.stdin.write(config)
                    await child.stdin.drain()
                    assert (
                        await asyncio.wait_for(child.stdout.readline(), 10)
                        == b"ready\n"
                    )
                assert len({child.pid for child in processes}) == 2
                output = await asyncio.wait_for(
                    asyncio.gather(*(p.communicate(b"start\n") for p in processes)), 10
                )
                assert sorted(p.returncode for p in processes) == (
                    [0, 17] if crash else [0, 0]
                ), output
                assert sorted(stdout.strip() for stdout, _ in output) == (
                    [b"", b"None"] if crash else [b"None", b"SUCCEEDED"]
                )
            finally:
                for child in processes:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
            if crash:
                await expire_synthetic_lease(db, ident)
            assert await flow.recover(await current_owner(db, ident)) is (
                ExecutionState.UNKNOWN if crash else ExecutionState.SUCCEEDED
            )
            assert await effects(db, ident) == ((0, 0, 7) if crash else (1, 1, 8))
            assert await flow.run_once(uuid4()) is None

    asyncio.run(check())


def test_restart_expired_effect_cannot_be_finalized_or_replayed(upgraded):
    async def check():
        async with restart_case(upgraded) as (
            db,
            approvals,
            store,
            adapter,
            flow,
            ident,
            keys,
        ):

            async def expired_worker():
                intent = await get_intent(db, approvals, ident)
                await expire_synthetic_lease(db, ident)
                # An authorized first invocation can arrive after lease loss.
                await adapter.invoke(intent)
                evidence = await adapter.evidence(intent)
                assert evidence and evidence.effect_version == 8
                _, unavailable, _, _ = services(db, [os.urandom(32), *keys[1:]])
                recovery = RestartFlow(unavailable, store, adapter)
                assert await asyncio.gather(
                    store.finalize_synthetic(evidence), recovery.recover(intent.owner)
                ) == [False, ExecutionState.UNKNOWN]
                raise RuntimeError("stop the expired worker")

            with pytest.raises(RuntimeError, match="stop the expired worker"):
                await flow.run_once(uuid4(), after_consumption=expired_worker)
            state, [attempt] = await snapshot(db, ident)
            assert state["state"] == attempt["outcome"] == "UNKNOWN"
            assert state["receipts"] == 0
            assert await flow.run_once(uuid4()) is None
            assert (
                await flow.recover(await current_owner(db, ident))
                is ExecutionState.UNKNOWN
            )
            assert await effects(db, ident) == (1, 1, 8)

    asyncio.run(check())


async def seed_wrong_targets(db):
    async with db.begin() as conn:
        await execute(
            conn,
            """
            INSERT INTO synthetic_restart.services
            SELECT workspace_id, customer_id, 'other-service', true, 30
            FROM synthetic_restart.services WHERE service_id='synthetic-search-api'
            """,
        )
        await execute(
            conn,
            """
            INSERT INTO synthetic_restart.services
            VALUES ('other-workspace','C-404','other-service',true,20)
            """,
        )


def wrong_target(intent, *, cross_tenant):
    scope = intent.scope
    if cross_tenant:
        scope = scope.model_copy(
            update={"workspace_id": "other-workspace", "customer_id": "C-404"}
        )
    return replace(
        intent,
        scope=scope,
        request=intent.request.model_copy(
            update={"customer_id": scope.customer_id, "service_id": "other-service"}
        ),
    )


async def target_generations(db, ident):
    async with db.connect() as conn:
        targets = await rows(
            conn,
            """
            SELECT customer_id, service_id, boot_generation FROM synthetic_restart.services
            ORDER BY customer_id, service_id
            """,
        )
        [counts] = await rows(
            conn,
            """
            SELECT count(*) AS reservations,
                   count(boot_generation) AS effects
            FROM synthetic_restart.outcomes WHERE operation_id=:id
            """,
            id=ident,
        )
    return [target["boot_generation"] for target in targets], counts


@pytest.mark.parametrize("cross_tenant", [False, True])
def test_restart_wrong_first_arrival_cannot_reserve(upgraded, cross_tenant):
    async def check():
        async with restart_case(upgraded) as (
            db,
            approvals,
            _,
            adapter,
            flow,
            ident,
            _,
        ):
            await seed_wrong_targets(db)

            async def invalid_first():
                intent = await get_intent(db, approvals, ident)
                with pytest.raises(
                    CommandStoreError, match="^SYNTHETIC_EVIDENCE_MISMATCH$"
                ):
                    await adapter.invoke(
                        wrong_target(intent, cross_tenant=cross_tenant)
                    )
                assert await target_generations(db, ident) == (
                    [30, 7, 20],
                    {"reservations": 0, "effects": 0},
                )

            assert (
                await flow.run_once(uuid4(), after_consumption=invalid_first)
                is ExecutionState.SUCCEEDED
            )
            assert await target_generations(db, ident) == (
                [30, 8, 20],
                {"reservations": 1, "effects": 1},
            )
            assert (await snapshot(db, ident))[0]["receipts"] == 1

    asyncio.run(check())


_ARRIVAL_CHILD = """
import asyncio, json, sys
from sqlalchemy.ext.asyncio import create_async_engine
from gateway.execution_contracts import ExecutionOwner, ProtectedIntentScope
from gateway.execution_store import CommandStoreError
from synthetic_it_restart import RestartIntent, RestartLedger, RestartRequest

async def main():
    item = json.loads(await asyncio.to_thread(sys.stdin.readline))
    db = create_async_engine(item["url"], hide_parameters=True)
    try:
        intent = RestartIntent(
            ExecutionOwner.model_validate(item["owner"]),
            ProtectedIntentScope.model_validate(item["scope"]),
            RestartRequest.model_validate(item["request"]),
            bytes.fromhex(item["sealed"]),
        )
        print("ready", flush=True)
        assert await asyncio.to_thread(sys.stdin.readline) == "start\\n"
        try:
            await RestartLedger(db, bytes.fromhex(item["key"])).invoke(intent)
        except CommandStoreError as error:
            print(str(error), flush=True)
        else:
            print("accepted", flush=True)
    finally:
        await db.dispose()

asyncio.run(main())
"""


@pytest.mark.parametrize("cross_tenant", [False, True])
def test_restart_races_valid_and_invalid_adapter_processes(upgraded, cross_tenant):
    async def check():
        async with restart_case(upgraded) as (db, approvals, _, _, flow, ident, keys):
            await seed_wrong_targets(db)

            async def race():
                valid = await get_intent(db, approvals, ident)
                invalid = wrong_target(valid, cross_tenant=cross_tenant)
                children = []
                try:
                    for intent in (invalid, valid):
                        child = await asyncio.create_subprocess_exec(
                            sys.executable,
                            "-c",
                            _ARRIVAL_CHILD,
                            env={
                                **os.environ,
                                "PYTHONPATH": str(Path(__file__).parent),
                            },
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.STDOUT,
                        )
                        children.append(child)
                        config = (
                            json.dumps(
                                {
                                    "url": db.url.render_as_string(hide_password=False),
                                    "key": keys[-1].hex(),
                                    "owner": intent.owner.model_dump(mode="json"),
                                    "scope": intent.scope.model_dump(mode="json"),
                                    "request": intent.request.model_dump(mode="json"),
                                    "sealed": intent.sealed_digest.hex(),
                                }
                            ).encode()
                            + b"\n"
                        )
                        child.stdin.write(config)
                        await child.stdin.drain()
                        assert (
                            await asyncio.wait_for(child.stdout.readline(), 10)
                            == b"ready\n"
                        )
                    assert children[0].pid != children[1].pid
                    output = await asyncio.wait_for(
                        asyncio.gather(
                            *(child.communicate(b"start\n") for child in children)
                        ),
                        10,
                    )
                    assert all(child.returncode == 0 for child in children), output
                    assert [stdout.strip() for stdout, _ in output] == [
                        b"SYNTHETIC_EVIDENCE_MISMATCH",
                        b"accepted",
                    ]
                finally:
                    for child in children:
                        if child.returncode is None:
                            child.kill()
                            await child.wait()
                assert await target_generations(db, ident) == (
                    [30, 8, 20],
                    {"reservations": 1, "effects": 1},
                )

            assert (
                await flow.run_once(uuid4(), after_consumption=race)
                is ExecutionState.SUCCEEDED
            )
            assert await target_generations(db, ident) == (
                [30, 8, 20],
                {"reservations": 1, "effects": 1},
            )
            assert (await snapshot(db, ident))[0]["receipts"] == 1

    asyncio.run(check())


def test_restart_adapter_requires_consumption_and_current_attempt(
    upgraded, monkeypatch
):
    async def check():
        async with restart_case(upgraded) as (
            db,
            approvals,
            store,
            adapter,
            flow,
            ident,
            _,
        ):
            consume = store.consume_synthetic_send

            async def reject_before_consumption(marker, **kwargs):
                intent = await get_intent(db, approvals, ident)
                for candidate in (
                    intent,
                    replace(
                        intent,
                        owner=intent.owner.model_copy(
                            update={"generation": intent.owner.generation + 1}
                        ),
                    ),
                ):
                    with pytest.raises(
                        CommandStoreError, match="^SYNTHETIC_RESTART_NOT_AUTHORIZED$"
                    ):
                        await adapter.invoke(candidate)
                assert await effects(db, ident) == (2, 0, 7)
                async with db.connect() as conn:
                    assert (
                        await rows(conn, "SELECT * FROM synthetic_restart.outcomes")
                        == []
                    )
                return await consume(marker, **kwargs)

            monkeypatch.setattr(
                store, "consume_synthetic_send", reject_before_consumption
            )
            assert await flow.run_once(uuid4()) is ExecutionState.SUCCEEDED
            assert await effects(db, ident) == (3, 1, 8)

    asyncio.run(check())


@pytest.mark.parametrize("marker_fault", ["missing", "malformed", "generic", "refund"])
def test_restart_requires_persisted_restart_contract(upgraded, marker_fault):
    async def check():
        async with restart_case(upgraded) as (
            db,
            approvals,
            _,
            adapter,
            flow,
            ident,
            _,
        ):

            async def corrupt_authority():
                intent = await get_intent(db, approvals, ident)
                async with db.begin() as conn:
                    [marker] = await rows(
                        conn,
                        """
                        SELECT id, actor, action FROM audit_events
                        WHERE resource_id=:id AND starts_with(action,'SYNTHETIC_SEND_MARKED:')
                        """,
                        id=ident,
                    )
                    # Only the trusted fixture owner can inject these states.
                    await execute(
                        conn,
                        "ALTER TABLE audit_events DISABLE TRIGGER protect_synthetic_consumption_rows",
                    )
                    if marker_fault == "missing":
                        await execute(
                            conn,
                            "DELETE FROM audit_events WHERE id=:id",
                            id=marker["id"],
                        )
                    else:
                        action = {
                            "malformed": "SYNTHETIC_SEND_MARKED:it.restart_service:it-restart-ledger.v1:bad",
                            "generic": "SYNTHETIC_SEND_MARKED",
                            "refund": "SYNTHETIC_SEND_MARKED:payments.issue_refund:refund-ledger.v2:"
                            + intent.binding.hex(),
                        }[marker_fault]
                        await execute(
                            conn,
                            "UPDATE audit_events SET action=:action WHERE id=:id",
                            action=action,
                            id=marker["id"],
                        )
                    await execute(
                        conn,
                        "ALTER TABLE audit_events ENABLE ALWAYS TRIGGER protect_synthetic_consumption_rows",
                    )
                with pytest.raises(
                    CommandStoreError, match="^SYNTHETIC_EVIDENCE_MISMATCH$"
                ):
                    await adapter.invoke(intent)
                assert await effects(db, ident) == (1, 0, 7)
                async with db.begin() as conn:
                    assert (
                        await rows(conn, "SELECT * FROM synthetic_restart.outcomes")
                        == []
                    )
                    await execute(
                        conn,
                        "ALTER TABLE audit_events DISABLE TRIGGER protect_synthetic_consumption_rows",
                    )
                    if marker_fault == "missing":
                        await execute(
                            conn,
                            "INSERT INTO audit_events(actor,action,resource_id) VALUES (:actor,:action,:id)",
                            actor=marker["actor"],
                            action=marker["action"],
                            id=ident,
                        )
                    else:
                        await execute(
                            conn,
                            "UPDATE audit_events SET action=:action WHERE id=:id",
                            action=marker["action"],
                            id=marker["id"],
                        )
                    await execute(
                        conn,
                        "ALTER TABLE audit_events ENABLE ALWAYS TRIGGER protect_synthetic_consumption_rows",
                    )

            assert (
                await flow.run_once(uuid4(), after_consumption=corrupt_authority)
                is ExecutionState.SUCCEEDED
            )
            assert await effects(db, ident) == (2, 1, 8)

    asyncio.run(check())
