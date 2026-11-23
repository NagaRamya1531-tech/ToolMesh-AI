"""Discovery and verification through the standalone sandbox worker process."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncTransaction, create_async_engine
from test_durable_execution_schema import IMAGE
from test_durable_execution_schema import sandbox as sandbox
from test_execution_store import REQUEST
from test_execution_worker_lifecycle_postgres import (
    PROVISION,
    event_from,
    start_worker,
    stop_worker,
)
from test_execution_worker_postgres import expire_synthetic_lease, snapshot
from test_synthetic_refund_postgres import effect, services
from test_synthetic_refund_postgres import upgraded as upgraded
from test_synthetic_refund_reconciliation_postgres import (
    check_terminal,
    make_due,
    parked,
)

from gateway.execution_contracts import ExecutionState
from gateway.execution_store import CommandStoreError
from gateway.sandbox_refund_reconciliation import RefundReconciler
from gateway.sandbox_refund_store import SandboxRefundStore
from shared.database import execute, rows

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires disposable PostgreSQL image"
)
GRANTS = Path(__file__).with_name("fixtures") / "m914_refund_verifier_privileges.sql"
LOGIN = "m914_verifier_test"
PASSWORD = "disposable-refund-verifier"


@asynccontextmanager
async def worker_login(db):
    async with db.begin() as conn:
        driver = (await conn.get_raw_connection()).driver_connection
        await driver.execute(PROVISION.read_text())
        await driver.execute(GRANTS.read_text())
        await execute(
            conn,
            f"CREATE ROLE {LOGIN} LOGIN PASSWORD '{PASSWORD}' "
            "IN ROLE m914_worker_access, m914_refund_verifier",
        )
        await execute(conn, f"GRANT CONNECT ON DATABASE toolmesh TO {LOGIN}")
    worker_db = create_async_engine(
        db.url.set(username=LOGIN, password=PASSWORD), hide_parameters=True
    )
    try:
        yield worker_db
    finally:
        await worker_db.dispose()
        async with db.begin() as conn:
            await execute(conn, "DROP SCHEMA m914_refund CASCADE")
            await execute(conn, "DROP SCHEMA m914_ownership CASCADE")
            for role in (
                LOGIN,
                "m914_refund_verifier",
                "m914_refund_function_owner",
                "m914_worker_access",
                "m914_function_owner",
            ):
                await execute(conn, f"DROP OWNED BY {role}")
                await execute(conn, f"DROP ROLE {role}")


def key_setting(keys):
    return ":".join(key.hex() for key in keys)


def test_due_discovery_and_fenced_lease_recovery(upgraded, monkeypatch):
    async def check():
        async with parked(upgraded, monkeypatch) as fixture:
            db, store, _, _, _, sender, send, lookup, intents, keys = fixture
            ident = sender.execution_id
            async with worker_login(db) as worker_db:
                _, approvals, _, _ = services(worker_db, keys)
                worker_store = SandboxRefundStore(
                    worker_db, approvals, timedelta(seconds=30), keys[-1]
                )
                await worker_store.validate_access()
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        UPDATE execution_commands SET next_check_at=clock_timestamp()+interval '1 hour'
                        WHERE execution_id=:id
                        """,
                        id=ident,
                    )
                assert await worker_store.discover_due_synthetic_refunds() == []
                await make_due(db, ident)
                before = await snapshot(db, ident)
                assert await asyncio.gather(
                    worker_store.discover_due_synthetic_refunds(),
                    worker_store.discover_due_synthetic_refunds(),
                ) == [[ident], [ident]]
                assert await snapshot(db, ident) == before
                claims = await asyncio.gather(
                    worker_store.claim_synthetic_refund_verification(ident, uuid4()),
                    worker_store.claim_synthetic_refund_verification(ident, uuid4()),
                )
                [stale] = [claim for claim in claims if claim is not None]
                assert await worker_store.discover_due_synthetic_refunds() == []
                await expire_synthetic_lease(db, ident)
                assert await worker_store.discover_due_synthetic_refunds() == [ident]
                evidence = await lookup(intents[0])
                assert evidence is not None
                assert (
                    await worker_store.finish_synthetic_refund_verification(
                        stale,
                        evidence=evidence,
                        binding_preimage=intents[0].binding_preimage(),
                    )
                    is None
                )
                first, second = await asyncio.gather(
                    *(
                        start_worker(
                            worker_db, refund_verification_keys=key_setting(keys)
                        )
                        for _ in range(2)
                    )
                )
                try:
                    async with asyncio.timeout(10):
                        while (await snapshot(db, ident))[0]["state"] != "SUCCEEDED":
                            assert (
                                first.returncode is None and second.returncode is None
                            )
                            await asyncio.sleep(0.05)
                    await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
                    assert await effect(db, ident) == (1, 123, 377)
                    assert (await snapshot(db, ident))[1][1]["outcome"] == "UNKNOWN"
                finally:
                    assert await asyncio.gather(
                        stop_worker(first), stop_worker(second)
                    ) == [0, 0]

    asyncio.run(check())


def test_worker_prioritizes_send_and_retries_missing_evidence(upgraded, monkeypatch):
    async def check():
        async with parked(upgraded, monkeypatch, committed=False) as fixture:
            db, store, ledger, approvals, _, sender, send, lookup, _, keys = fixture
            ident = sender.execution_id
            intents = []

            async def observe(intent):
                intents.append(intent)
                return await lookup(intent)

            # Capture the original request for later delivery, then make the
            # worker discover it as unresolved.
            assert (
                await RefundReconciler(approvals, store, observe).run_once(
                    ident, uuid4()
                )
                is ExecutionState.UNKNOWN
            )
            await make_due(db, ident)
            commands = services(db, keys)[0]
            pending = await commands.submit(
                replace(REQUEST, idempotency_key=str(uuid4()))
            )
            await approvals.decide(
                pending.execution_id, "refund-approver", "APPROVED", "fixture refund"
            )
            async with worker_login(db) as engine:
                async with engine.connect() as conn:
                    [permissions] = await rows(
                        conn,
                        """
                        SELECT has_table_privilege('synthetic_refund.outcomes','INSERT,UPDATE,DELETE') AS outcomes_write,
                               has_table_privilege('synthetic_refund.arrivals','INSERT') AS arrivals_write,
                               has_table_privilege('synthetic_refund.captures','UPDATE') AS capture_write
                    """,
                    )
                    assert not any(permissions.values())
                worker = await start_worker(
                    engine, refund_verification_keys=key_setting(keys)
                )
                try:
                    line = await event_from(worker, b"ownership_claimed")
                    assert str(pending.execution_id).encode() in line
                    assert b"UNKNOWN" in await event_from(worker, b"refund_verified")
                    held, [send_attempt] = await snapshot(db, pending.execution_id)
                    assert (
                        held["state"] == "CLAIMED" and send_attempt["outcome"] is None
                    )
                    assert held["lease_owner"] is not None
                    # Keep another SEND eligible while the same process renews
                    # its first SEND and verifies twice.
                    waiting = await commands.submit(
                        replace(REQUEST, idempotency_key=str(uuid4()))
                    )
                    await approvals.decide(
                        waiting.execution_id,
                        "refund-approver",
                        "APPROVED",
                        "fixture refund",
                    )
                    await event_from(worker, b"ownership_renewed")
                    assert await effect(db, ident) == (0, None, 500)
                    await ledger.invoke(intents[0])
                    await make_due(db, ident)
                    assert b"SUCCEEDED" in await event_from(worker, b"refund_verified")
                    current, [current_send] = await snapshot(db, pending.execution_id)
                    assert current["lease_owner"] == held["lease_owner"]
                    assert current["generation"] == held["generation"]
                    assert current_send == send_attempt
                    assert (await snapshot(db, waiting.execution_id))[0][
                        "state"
                    ] == "READY"
                    await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
                    assert await effect(db, ident) == (1, 123, 377)
                finally:
                    assert await stop_worker(worker) == 0
                assert (await snapshot(db, pending.execution_id))[0]["state"] == "READY"

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["lookup", "claim", "lease-loss"])
def test_worker_shutdown_preserves_verification_recovery(upgraded, monkeypatch, phase):
    async def check():
        async with parked(upgraded, monkeypatch) as fixture:
            db, _, _, approvals, _, sender, send, _, _, keys = fixture
            ident = sender.execution_id
            if phase == "lease-loss":
                commands = services(db, keys)[0]
                pending = await commands.submit(
                    replace(REQUEST, idempotency_key=str(uuid4()))
                )
                await approvals.decide(
                    pending.execution_id, "refund-approver", "APPROVED", "lease loss"
                )
            # Hold the process after a committed claim, either before its
            # acknowledgment or during the read-only lookup.
            program = """
import asyncio
from gateway import execution_worker as worker
from gateway.sandbox_refund_store import SandboxRefundStore
original_claim = SandboxRefundStore.claim_synthetic_refund_verification

async def hold():
    print('verification_hold', flush=True)
    await asyncio.Event().wait()

async def claim(self, *args):
    await original_claim(self, *args)
    await hold()

async def lookup(*args):
    await hold()
"""
            program += (
                "\nSandboxRefundStore.claim_synthetic_refund_verification = claim"
                if phase == "claim"
                else "\nworker.read_refund_evidence = lookup"
            )
            program += "\nraise SystemExit(worker.main())\n"
            async with worker_login(db) as engine:
                proc = await start_worker(
                    engine,
                    program=(program,),
                    drain_seconds=0.2,
                    refund_verification_keys=key_setting(keys),
                )
                try:
                    await event_from(proc, b"verification_hold")
                    if phase == "lease-loss":
                        await expire_synthetic_lease(db, pending.execution_id)
                        await event_from(proc, b"ownership_lost")
                        assert await asyncio.wait_for(proc.wait(), 5) == 0
                finally:
                    assert await stop_worker(proc) == (3 if phase == "claim" else 0)
                state, [original, verify] = await snapshot(db, ident)
                assert original == send and verify["outcome"] is None
                assert state["state"] == "UNKNOWN" and state["receipts"] == 0
                await expire_synthetic_lease(db, ident)
                proc = await start_worker(
                    engine, refund_verification_keys=key_setting(keys)
                )
                try:
                    assert b"SUCCEEDED" in await event_from(proc, b"refund_verified")
                    await check_terminal(db, sender, send, ExecutionState.SUCCEEDED)
                    assert await effect(db, ident) == (1, 123, 377)
                finally:
                    assert await stop_worker(proc) == 0

    asyncio.run(check())


def test_verifier_denies_direct_writes_and_rejects_excess_grants(upgraded, monkeypatch):
    async def check():
        async with parked(upgraded, monkeypatch) as fixture:
            db, _, _, approvals, _, sender, _, _, intents, keys = fixture
            ident = sender.execution_id
            commands = services(db, keys)[0]
            other = await commands.submit(
                replace(
                    REQUEST,
                    idempotency_key=str(uuid4()),
                    requester="it-agent",
                    tool_id="it.restart_service",
                    arguments={
                        "customer_id": "C-102",
                        "service_id": "synthetic-search-api",
                    },
                )
            )
            await approvals.decide(
                other.execution_id, "refund-approver", "APPROVED", "scope check"
            )
            async with worker_login(db) as engine:
                store = SandboxRefundStore(
                    engine, approvals, timedelta(seconds=30), keys[-1]
                )
                await store.validate_access()
                assert (
                    await store.claim_synthetic_refund_verification(
                        other.execution_id, uuid4()
                    )
                    is None
                )
                for target in (ident, other.execution_id):
                    for statement in (
                        "INSERT INTO execution_attempts(execution_id,generation,worker_id,kind,claimed_at) VALUES (:id,99,:worker,'VERIFY',now())",
                        "UPDATE execution_attempts SET outcome='SUCCEEDED',finished_at=now() WHERE execution_id=:id",
                        "DELETE FROM execution_attempts WHERE execution_id=:id",
                        "INSERT INTO execution_receipts(execution_id,generation,outcome,evidence_source,external_reference,evidence_digest) VALUES (:id,99,'SUCCEEDED','LOOKUP','forged',sha256(''::bytea))",
                        "UPDATE execution_receipts SET evidence_source='LOOKUP' WHERE execution_id=:id",
                        "DELETE FROM execution_receipts WHERE execution_id=:id",
                        "UPDATE execution_commands SET state='SUCCEEDED' WHERE execution_id=:id",
                        "UPDATE executions SET status='SUCCEEDED' WHERE id=:id",
                        "DELETE FROM executions WHERE id=:id",
                        "INSERT INTO synthetic_refund.arrivals VALUES (:id)",
                        "UPDATE synthetic_refund.outcomes SET refunded_minor=0 WHERE operation_id=:id",
                        "DELETE FROM synthetic_refund.outcomes WHERE operation_id=:id",
                    ):
                        with pytest.raises(DBAPIError) as denied:
                            async with engine.begin() as conn:
                                await execute(
                                    conn, statement, id=target, worker=uuid4()
                                )
                        assert denied.value.orig.sqlstate == "42501"

                # Call the PostgreSQL interface directly to bypass Python validation.
                assert (
                    claim := await store.claim_synthetic_refund_verification(
                        ident, uuid4()
                    )
                ) is not None
                operation = """
                    SELECT * FROM m914_refund.operate('finish',:id,:worker,:gen,
                        NULL,:preimage,:verdict,NULL,NULL)
                """
                owner = claim.lease.owner
                params = {
                    "id": ident,
                    "worker": owner.worker_id,
                    "gen": owner.generation,
                    "preimage": intents[0].binding_preimage(),
                    "verdict": "EFFECT_COMMITTED",
                }
                async with engine.begin() as conn:
                    assert not await rows(
                        conn, operation, **(params | {"gen": owner.generation - 1})
                    )
                    assert not await rows(
                        conn, operation, **(params | {"worker": uuid4()})
                    )
                for invalid in (
                    {"preimage": b"bad"},
                    {"verdict": "DEFINITIVE_NO_EFFECT"},
                ):
                    with pytest.raises(DBAPIError) as denied:
                        async with engine.begin() as conn:
                            await rows(conn, operation, **(params | invalid))
                    assert denied.value.orig.sqlstate == "M9147"
                state, attempts = await snapshot(db, ident)
                assert state["receipts"] == 0 and attempts[-1]["outcome"] is None
                assert (await snapshot(db, other.execution_id))[0]["state"] == "READY"

                async with db.begin() as conn:
                    await execute(
                        conn,
                        f"GRANT UPDATE (refunded_minor) ON synthetic_refund.outcomes TO {LOGIN}",
                    )
                with pytest.raises(
                    CommandStoreError, match="WORKER_VERIFIER_ACCESS_REJECTED"
                ):
                    await store.validate_access()
                proc = await start_worker(
                    engine, refund_verification_keys=key_setting(keys)
                )
                assert b"WORKER_VERIFIER_ACCESS_REJECTED" in await event_from(
                    proc, b"worker_stopped"
                )
                assert await asyncio.wait_for(proc.wait(), 5) == 2
                async with db.begin() as conn:
                    await execute(
                        conn,
                        f"REVOKE UPDATE (refunded_minor) ON synthetic_refund.outcomes FROM {LOGIN}",
                    )
                    await execute(conn, f"REVOKE m914_refund_verifier FROM {LOGIN}")
                with pytest.raises(
                    CommandStoreError, match="WORKER_VERIFIER_ACCESS_REJECTED"
                ):
                    await store.validate_access()

    asyncio.run(check())


@pytest.mark.parametrize("seed", [True, False])
@pytest.mark.parametrize("fault", ["lost-ack", "rollback"])
def test_restricted_terminal_transaction_recovery(upgraded, monkeypatch, seed, fault):
    async def check():
        async with parked(upgraded, monkeypatch, seed=seed) as fixture:
            db, _, _, approvals, _, sender, send, lookup, _, keys = fixture
            ident = sender.execution_id
            expected = ExecutionState.SUCCEEDED if seed else ExecutionState.FAILED
            async with worker_login(db) as engine:
                store = SandboxRefundStore(
                    engine, approvals, timedelta(seconds=5), keys[-1]
                )
                original = store.finish_synthetic_refund_verification
                original_exit = AsyncTransaction.__aexit__

                async def fault_exit(transaction, *failure):
                    assert failure[0] is None
                    if fault == "lost-ack":
                        await original_exit(transaction, *failure)
                    else:
                        await transaction.rollback()
                    raise OSError("fixture transaction fault")

                async def interrupt(*args, **kwargs):
                    with monkeypatch.context() as patch:
                        patch.setattr(AsyncTransaction, "__aexit__", fault_exit)
                        return await original(*args, **kwargs)

                reconciler = RefundReconciler(approvals, store, lookup)
                with monkeypatch.context() as patch:
                    patch.setattr(
                        store, "finish_synthetic_refund_verification", interrupt
                    )
                    if fault == "lost-ack":
                        assert await reconciler.run_once(ident, uuid4()) is expected
                    else:
                        with pytest.raises(
                            CommandStoreError, match="EXECUTION_OWNERSHIP_UNAVAILABLE"
                        ):
                            await reconciler.run_once(ident, uuid4())
                if fault == "rollback":
                    state, [original_send, verify] = await snapshot(db, ident)
                    assert original_send == send and verify["outcome"] is None
                    assert state["state"] == state["status"] == "UNKNOWN"
                    assert state["receipts"] == 0
                    assert await reconciler.run_once(ident, uuid4()) is None
                    await expire_synthetic_lease(db, ident)
                    assert await reconciler.run_once(ident, uuid4()) is expected
                assert await reconciler.run_once(ident, uuid4()) is None
                await check_terminal(db, sender, send, expected)
                assert await effect(db, ident) == (
                    (1, 123, 377) if seed else (1, 0, None)
                )

    asyncio.run(check())
