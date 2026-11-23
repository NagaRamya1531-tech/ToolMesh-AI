"""Ownership tests use synthetic commands and disposable PostgreSQL only."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from hmac import digest as hmac_digest
from os import urandom
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncTransaction, create_async_engine
from synthetic_execution_adapter import SyntheticAdapter, SyntheticHarness
from test_durable_execution_schema import IMAGE, migrate
from test_durable_execution_schema import sandbox as sandbox
from test_execution_store import REQUEST, ledger

from gateway.execution_approval import ExecutionApprovals
from gateway.execution_contracts import ExecutionOwner, ExecutionState
from gateway.execution_store import CommandStoreError
from gateway.execution_worker_store import (
    ExecutionWorkerStore,
    synthetic_evidence_bytes,
)
from shared.database import execute, rows

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires disposable PostgreSQL image"
)
ROLE = "toolmesh_ownership_test"
PASSWORD = "synthetic-worker-test"


@pytest.fixture(scope="module")
def upgraded(sandbox):
    _, env, _ = sandbox
    migrate(env, "upgrade", "0011")
    return sandbox


@asynccontextmanager
async def ownership(upgraded):
    async with ledger(upgraded) as (db, commands, payloads, digests):
        async with db.begin() as conn:
            await execute(conn, f"CREATE ROLE {ROLE} LOGIN PASSWORD '{PASSWORD}'")
            await execute(conn, f"GRANT CONNECT ON DATABASE toolmesh TO {ROLE}")
            await execute(conn, f"GRANT USAGE ON SCHEMA public TO {ROLE}")
            await execute(
                conn,
                f"""GRANT SELECT (id, status), UPDATE (id) ON approvals TO {ROLE};
                """,
            )
            await execute(
                conn,
                f"""GRANT SELECT (id, status), UPDATE (status, error_code, finished_at)
                    ON executions TO {ROLE}""",
            )
            for table in ("execution_dispatch_gate", "execution_authorization_guard"):
                # PostgreSQL requires UPDATE on at least one column for a row lock.
                # The singleton CHECK prevents assigning any different value.
                await execute(
                    conn, f"GRANT SELECT, UPDATE (singleton) ON {table} TO {ROLE}"
                )
            await execute(
                conn,
                f"""GRANT SELECT (execution_id, state, generation, scheduled_at,
                                  dispatch_not_after, lease_owner, lease_expires_at),
                          UPDATE (state, generation, lease_owner, lease_expires_at)
                    ON execution_commands TO {ROLE}""",
            )
            await execute(
                conn,
                f"""GRANT SELECT, INSERT (execution_id, generation, worker_id, kind, claimed_at),
                          UPDATE (finished_at, outcome) ON execution_attempts TO {ROLE}""",
            )
        worker_db = create_async_engine(
            make_url(str(db.url)).set(username=ROLE, password=PASSWORD),
            hide_parameters=True,
        )
        approvals = ExecutionApprovals(db, payloads, digests, timedelta(minutes=3))

        async def ready():
            submission = await commands.submit(
                replace(REQUEST, idempotency_key=str(uuid4()))
            )
            await approvals.decide(
                submission.execution_id,
                "refund-approver",
                "APPROVED",
                "synthetic approval",
            )
            return submission.execution_id

        try:
            yield (
                db,
                worker_db,
                ExecutionWorkerStore(worker_db, timedelta(seconds=30)),
                ready,
            )
        finally:
            await worker_db.dispose()
            async with db.begin() as conn:
                await execute(conn, "DELETE FROM execution_receipts")
                await execute(conn, "DELETE FROM execution_attempts")
                await execute(conn, "DELETE FROM execution_approval_decisions")
                await execute(conn, f"DROP OWNED BY {ROLE}")
                await execute(conn, f"DROP ROLE {ROLE}")


async def snapshot(db, ident):
    async with db.connect() as conn:
        [state] = await rows(
            conn,
            """
            SELECT c.state, c.generation, c.lease_owner, c.lease_expires_at,
                   e.status, e.error_code, a.status AS approval,
                   to_jsonb(c) AS command_row, to_jsonb(e) AS execution_row,
                   to_jsonb(a) AS approval_row,
                   (SELECT count(*) FROM execution_events WHERE execution_id=:id) AS events,
                   (SELECT count(*) FROM audit_events WHERE resource_id=:id) AS audit,
                   (SELECT count(*) FROM execution_receipts WHERE execution_id=:id) AS receipts,
                   (SELECT jsonb_agg(to_jsonb(ev) ORDER BY ev.id)
                    FROM execution_events ev WHERE ev.execution_id=:id) AS event_rows,
                   (SELECT jsonb_agg(to_jsonb(au) ORDER BY au.id)
                    FROM audit_events au WHERE au.resource_id=:id) AS audit_rows,
                   (SELECT to_jsonb(r) FROM execution_receipts r WHERE r.execution_id=:id) AS receipt_row
            FROM execution_commands c JOIN executions e ON e.id=c.execution_id
            JOIN approvals a ON a.id=e.id WHERE e.id=:id
            """,
            id=ident,
        )
        attempts = await rows(
            conn,
            "SELECT * FROM execution_attempts WHERE execution_id=:id ORDER BY generation",
            id=ident,
        )
    return state, attempts


def test_exclusive_claim_renewal_recovery_and_stale_fence(upgraded):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            ident = await ready()
            barrier = asyncio.Barrier(8)

            async def claim():
                await barrier.wait()
                return await store.claim(uuid4())

            results = await asyncio.gather(*(claim() for _ in range(8)))
            [lease] = [item for item in results if item is not None]
            assert lease.owner.execution_id == ident and lease.owner.generation == 1
            state, [attempt] = await snapshot(db, ident)
            assert state["status"] == "RUNNING" and state["state"] == "CLAIMED"
            assert (
                attempt["send_marked_at"] is None
                and attempt["worker_id"] == lease.owner.worker_id
            )
            wrong_owner = ExecutionOwner(
                execution_id=ident, worker_id=uuid4(), generation=1
            )
            assert await store.renew(wrong_owner) is None
            assert await store.release(wrong_owner) is None
            renewed = await store.renew(lease.owner)
            assert renewed and renewed.expires_at > lease.expires_at
            assert await store.recover_next() is None
            async with db.begin() as conn:
                await execute(
                    conn,
                    """
                    UPDATE execution_commands SET lease_expires_at=clock_timestamp()-interval '1 second'
                    WHERE execution_id=:id
                    """,
                    id=ident,
                )
            assert await store.renew(lease.owner) is None
            assert await store.release(lease.owner) is None
            recovered = await store.recover_next()
            assert recovered and recovered.state is ExecutionState.READY
            assert recovered.generation == 1
            # Reuse the UUID deliberately to exercise generation fencing separately.
            replacement = await store.claim(lease.owner.worker_id)
            assert replacement and replacement.owner.generation == 2
            before = await snapshot(db, ident)
            assert await store.renew(lease.owner) is None
            assert await store.release(lease.owner) is None
            assert await snapshot(db, ident) == before
            released = await store.release(replacement.owner)
            assert released and released.state is ExecutionState.READY
            final, attempts = await snapshot(db, ident)
            assert final["lease_owner"] is None and final["lease_expires_at"] is None
            assert (
                final["events"] == state["events"] and final["audit"] == state["audit"]
            )
            assert final["receipts"] == 0 and final["approval"] == "APPROVED"
            assert [
                (a["generation"], a["outcome"], a["send_marked_at"]) for a in attempts
            ] == [(1, "NOT_SENT", None), (2, "NOT_SENT", None)]

    asyncio.run(check())


def test_claim_skips_locked_execution_and_future_schedule(upgraded):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            first, second, future = [await ready() for _ in range(3)]
            async with db.begin() as conn:
                await execute(
                    conn,
                    """
                    UPDATE execution_commands SET scheduled_at=clock_timestamp()+interval '1 hour'
                    WHERE execution_id=:id
                    """,
                    id=future,
                )
            async with db.begin() as conn:
                await rows(
                    conn, "SELECT id FROM executions WHERE id=:id FOR UPDATE", id=first
                )
                lease = await store.claim(uuid4())
                assert lease and lease.owner.execution_id == second
                assert await store.claim(uuid4()) is None
            lease = await store.claim(uuid4())
            assert lease and lease.owner.execution_id == first
            assert (await snapshot(db, future))[1] == []

    asyncio.run(check())


@pytest.mark.parametrize("claimed", [False, True])
def test_pre_send_deadline_projects_failed_atomically(upgraded, claimed):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            ident = await ready()
            if claimed:
                assert await store.claim(uuid4())
            async with db.begin() as conn:
                await execute(
                    conn,
                    """
                    UPDATE execution_commands SET
                    dispatch_not_after=clock_timestamp()-interval '1 second',
                    lease_expires_at=CASE WHEN lease_owner IS NOT NULL
                        THEN clock_timestamp()-interval '1 second' END
                    WHERE execution_id=:id
                    """,
                    id=ident,
                )
            assert await store.claim(uuid4()) is None
            closed = await store.recover_next()
            assert closed and closed.state is ExecutionState.FAILED
            state, attempts = await snapshot(db, ident)
            assert state["state"] == state["status"] == "FAILED"
            assert state["error_code"] == "DISPATCH_DEADLINE_EXCEEDED"
            assert state["lease_owner"] is None and state["approval"] == "APPROVED"
            assert [(a["outcome"], a["send_marked_at"]) for a in attempts] == (
                [("NOT_SENT", None)] if claimed else []
            )
            assert await store.recover_next() is None

    asyncio.run(check())


@pytest.mark.parametrize(
    "sql,code",
    [
        ("DELETE FROM execution_dispatch_gate", "EXECUTION_COORDINATION_UNAVAILABLE"),
        (
            "DELETE FROM execution_authorization_guard",
            "EXECUTION_COORDINATION_UNAVAILABLE",
        ),
        (
            "UPDATE execution_dispatch_gate SET mode='LEGACY'",
            "DURABLE_ADMISSION_DISABLED",
        ),
        (
            "UPDATE execution_dispatch_gate SET mode='PAUSED'",
            "DURABLE_ADMISSION_DISABLED",
        ),
    ],
)
def test_every_mutation_requires_durable_gate_and_guard(upgraded, sql, code):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            ident = await ready()
            pending = await ready()
            lease = await store.claim(uuid4())
            assert lease and lease.owner.execution_id == ident
            async with db.begin() as conn:
                await execute(conn, sql)
            snapshots = [await snapshot(db, item) for item in (ident, pending)]
            for operation, args in (
                (store.claim, (uuid4(),)),
                (store.renew, (lease.owner,)),
                (store.release, (lease.owner,)),
                (store.recover_next, ()),
            ):
                with pytest.raises(CommandStoreError, match=f"^{code}$"):
                    await operation(*args)
            assert [await snapshot(db, item) for item in (ident, pending)] == snapshots

    asyncio.run(check())


def test_attempt_insert_failure_rolls_back_claim(upgraded):
    async def check():
        async with ownership(upgraded) as (db, worker_db, store, ready):
            ident = await ready()
            before = await snapshot(db, ident)

            def reject_attempt(conn, cursor, statement, parameters, context, many):
                if statement.lstrip().startswith("INSERT INTO execution_attempts"):
                    cursor.execute("SELECT 1/0")

            event.listen(worker_db.sync_engine, "before_cursor_execute", reject_attempt)
            try:
                with pytest.raises(
                    CommandStoreError, match="^EXECUTION_OWNERSHIP_UNAVAILABLE$"
                ):
                    await store.claim(uuid4())
            finally:
                event.remove(
                    worker_db.sync_engine, "before_cursor_execute", reject_attempt
                )
            assert await snapshot(db, ident) == before
            lease = await store.claim(uuid4())
            assert lease and lease.owner.generation == 1

    asyncio.run(check())


def test_projection_failure_rolls_back_expired_claim_recovery(upgraded):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            ident = await ready()
            assert await store.claim(uuid4())
            async with db.begin() as conn:
                await execute(
                    conn,
                    """
                    UPDATE execution_commands SET
                        dispatch_not_after=clock_timestamp()-interval '1 second',
                        lease_expires_at=clock_timestamp()-interval '1 second'
                    WHERE execution_id=:id
                    """,
                    id=ident,
                )
                await execute(
                    conn,
                    "UPDATE executions SET status='UNKNOWN' WHERE id=:id",
                    id=ident,
                )
            before = await snapshot(db, ident)
            with pytest.raises(
                CommandStoreError, match="^EXECUTION_OWNERSHIP_CONFLICT$"
            ):
                await store.recover_next()
            assert await snapshot(db, ident) == before

    asyncio.run(check())


def test_disposable_role_privileges(upgraded):
    async def check():
        async with ownership(upgraded) as (db, worker_db, _, _):
            async with db.connect() as conn:
                [gateway] = await rows(
                    conn,
                    """
                    SELECT bool_and(
                        NOT has_any_column_privilege('toolmesh_app', name, 'SELECT,INSERT,UPDATE,REFERENCES')
                        AND NOT has_table_privilege('toolmesh_app', name, 'DELETE,TRUNCATE,TRIGGER')
                    ) AS ledger_denied,
                    has_table_privilege('toolmesh_app','execution_events','SELECT') AS legacy_events
                    FROM unnest(ARRAY['execution_commands','execution_attempts','execution_receipts',
                        'execution_request_keys','execution_legacy_key_aliases','execution_dispatch_gate',
                        'execution_authorization_guard','execution_approval_entitlements',
                        'execution_approval_decisions']) AS t(name)
                    """,
                )
            assert gateway == {"ledger_denied": True, "legacy_events": True}
            async with worker_db.connect() as conn:
                [privileges] = await rows(
                    conn,
                    """
                    SELECT current_user AS role,
                      has_column_privilege('execution_dispatch_gate','mode','UPDATE') AS gate,
                      has_column_privilege('execution_authorization_guard','revision','UPDATE') AS guard,
                      has_column_privilege('execution_commands','ciphertext','SELECT') AS ciphertext,
                      has_column_privilege('execution_attempts','send_marked_at','UPDATE') AS marker_update,
                      has_column_privilege('execution_attempts','send_marked_at','INSERT') AS marker_insert,
                      has_table_privilege('execution_receipts','INSERT') AS receipt,
                      has_column_privilege('approvals','status','UPDATE') AS approval,
                      has_table_privilege('policy_rules','UPDATE') AS policy
                    """,
                )
            assert privileges == {"role": ROLE} | dict.fromkeys(
                (
                    "gate",
                    "guard",
                    "ciphertext",
                    "marker_update",
                    "marker_insert",
                    "receipt",
                    "approval",
                    "policy",
                ),
                False,
            )

    asyncio.run(check())


def test_synthetic_marker_is_fenced_and_single_use(upgraded):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            ident = await ready()
            lease = await store.claim(uuid4())
            assert lease
            synthetic = ExecutionWorkerStore(
                db, timedelta(seconds=30), synthetic_key=urandom(32)
            )
            before = await snapshot(db, ident)
            with pytest.raises(
                CommandStoreError, match="^SYNTHETIC_EXECUTION_DISABLED$"
            ):
                await store.mark_synthetic_send(lease.owner)
            for wrong in (
                lease.owner.model_copy(update={"worker_id": uuid4()}),
                lease.owner.model_copy(
                    update={"generation": lease.owner.generation + 1}
                ),
                lease.owner.model_copy(update={"execution_id": uuid4()}),
            ):
                assert await synthetic.mark_synthetic_send(wrong) is None
            assert await snapshot(db, ident) == before
            results = await asyncio.gather(
                *(synthetic.mark_synthetic_send(lease.owner) for _ in range(4))
            )
            [marker] = [result for result in results if result]
            state, [attempt] = await snapshot(db, ident)
            assert (state["state"], state["status"]) == ("DISPATCHING", "RUNNING")
            assert state["events"] == before[0]["events"]
            assert state["audit"] == before[0]["audit"] + 1
            assert state["receipts"] == 0
            assert attempt["send_marked_at"] == marker.marked_at
            assert attempt["admission_revision"] == marker.admission_revision
            assert attempt["outcome"] is None

    asyncio.run(check())


@asynccontextmanager
async def synthetic_fixture(db):
    key = urandom(32)
    store = ExecutionWorkerStore(db, timedelta(seconds=30), synthetic_key=key)
    adapter = SyntheticAdapter(db, key)
    await adapter.initialize()
    try:
        yield store, adapter, SyntheticHarness(store, adapter)
    finally:
        await adapter.close()


@pytest.mark.parametrize("effect", [True, False])
def test_synthetic_finalization_and_concurrent_evidence(upgraded, effect):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, harness):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                evidence = await harness.send_if_current(marker, effect=effect)
                assert evidence
                assert await harness.send_if_current(marker, effect=effect) is None
                assert await adapter.counts(ident) == (1, int(effect))
                before = await snapshot(db, ident)
                wrong = evidence.model_copy(
                    update={
                        "result": (
                            "DEFINITIVE_NO_EFFECT" if effect else "EFFECT_COMMITTED"
                        )
                    }
                )
                with pytest.raises(
                    CommandStoreError, match="^SYNTHETIC_EVIDENCE_MISMATCH$"
                ):
                    await synthetic.finalize_synthetic(wrong)
                assert await snapshot(db, ident) == before
                result = await asyncio.gather(
                    synthetic.finalize_synthetic(wrong),
                    *(synthetic.finalize_synthetic(evidence) for _ in range(4)),
                    return_exceptions=True,
                )
                assert isinstance(result[0], CommandStoreError)
                assert str(result[0]) == "SYNTHETIC_EVIDENCE_MISMATCH"
                assert result.count(True) == 1
                state, [attempt] = await snapshot(db, ident)
                target = "SUCCEEDED" if effect else "FAILED"
                assert (state["state"], state["status"], attempt["outcome"]) == (
                    target,
                ) * 3
                assert state["lease_owner"] is None
                assert state["receipts"] == 1
                assert state["audit"] == before[0]["audit"] + 1
                assert state["events"] == before[0]["events"] + 1
                persisted = await synthetic.read_synthetic_attempt(
                    lease.owner, evidence
                )
                assert persisted and persisted["receipt_matches"]
                assert await adapter.counts(ident) == (1, int(effect))

    asyncio.run(check())


async def expire_synthetic_lease(db, ident):
    async with db.begin() as conn:
        await execute(
            conn,
            """
            UPDATE execution_commands SET lease_expires_at=clock_timestamp()-interval '1 second'
            WHERE execution_id=:id
            """,
            id=ident,
        )


@pytest.mark.parametrize("boundary", ["receipt", "projection", "event", "audit"])
def test_synthetic_finalization_rolls_back_at_each_write_boundary(upgraded, boundary):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, harness):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                evidence = await harness.send_if_current(marker, effect=True)
                assert evidence
                before = await snapshot(db, ident)
                statement_prefix = {
                    "receipt": "INSERT INTO execution_receipts",
                    "projection": "UPDATE executions SET",
                    "event": "UPDATE executions SET",
                    "audit": "INSERT INTO audit_events",
                }[boundary]

                def reject(conn, cursor, statement, parameters, context, many):
                    if boundary != "event" and statement.lstrip().startswith(
                        statement_prefix
                    ):
                        cursor.execute("SELECT 1/0")

                if boundary == "event":
                    async with db.begin() as conn:
                        await execute(
                            conn,
                            """
                            CREATE FUNCTION reject_synthetic_status_event() RETURNS trigger
                            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic status event fault';
                            END; $$
                            """,
                        )
                        await execute(
                            conn,
                            """
                            CREATE TRIGGER reject_synthetic_status_event BEFORE INSERT ON execution_events
                            FOR EACH ROW EXECUTE FUNCTION reject_synthetic_status_event()
                            """,
                        )
                event.listen(db.sync_engine, "before_cursor_execute", reject)
                try:
                    with pytest.raises(
                        CommandStoreError, match="^EXECUTION_OWNERSHIP_UNAVAILABLE$"
                    ):
                        await synthetic.finalize_synthetic(evidence)
                finally:
                    event.remove(db.sync_engine, "before_cursor_execute", reject)
                    if boundary == "event":
                        async with db.begin() as conn:
                            await execute(
                                conn,
                                "DROP TRIGGER reject_synthetic_status_event ON execution_events",
                            )
                            await execute(
                                conn, "DROP FUNCTION reject_synthetic_status_event()"
                            )
                assert await snapshot(db, ident) == before
                assert await adapter.counts(ident) == (1, 1)
                assert await synthetic.finalize_synthetic(evidence)
                assert await adapter.counts(ident) == (1, 1)

    asyncio.run(check())


@pytest.mark.parametrize("operation", ["mark", "finalize"])
@pytest.mark.parametrize("committed", [True, False])
def test_synthetic_lost_acknowledgment(upgraded, monkeypatch, operation, committed):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, harness):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                evidence = None
                if operation == "finalize":
                    marker = await synthetic.mark_synthetic_send(lease.owner)
                    assert marker
                    evidence = await harness.send_if_current(marker, effect=True)
                    assert evidence
                before = await snapshot(db, ident)
                exit_transaction = AsyncTransaction.__aexit__

                async def lose_ack(transaction, *failure):
                    if committed:
                        await exit_transaction(transaction, *failure)
                    else:
                        await transaction.rollback()
                    raise OSError("synthetic lost acknowledgment")

                with monkeypatch.context() as patch:
                    patch.setattr(AsyncTransaction, "__aexit__", lose_ack)
                    with pytest.raises(
                        CommandStoreError, match="^EXECUTION_OWNERSHIP_UNAVAILABLE$"
                    ):
                        if evidence is None:
                            await synthetic.mark_synthetic_send(lease.owner)
                        else:
                            await synthetic.finalize_synthetic(evidence)
                persisted = await synthetic.read_synthetic_attempt(
                    lease.owner, evidence
                )
                assert persisted
                if not committed:
                    assert await snapshot(db, ident) == before
                if evidence is None:
                    assert persisted["marked"] is committed
                    assert persisted["state"] is (
                        ExecutionState.DISPATCHING
                        if committed
                        else ExecutionState.CLAIMED
                    )
                    if committed:
                        assert await synthetic.mark_synthetic_send(lease.owner) is None
                        await expire_synthetic_lease(db, ident)
                        assert await synthetic.classify_synthetic_unknown(
                            lease.owner, expired=True
                        )
                    assert await adapter.counts(ident) == (0, 0)
                else:
                    assert persisted["receipt_matches"] is committed
                    if not committed:
                        assert await synthetic.finalize_synthetic(evidence)
                    assert not await synthetic.finalize_synthetic(evidence)
                    assert await adapter.counts(ident) == (1, 1)

    asyncio.run(check())


def test_synthetic_late_first_send_has_no_replacement(upgraded):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, harness):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                checked = asyncio.Event()
                resume = asyncio.Event()

                async def pause():
                    checked.set()
                    await resume.wait()

                sender = asyncio.create_task(
                    harness.send_if_current(marker, effect=True, before_invoke=pause)
                )
                try:
                    await asyncio.wait_for(checked.wait(), timeout=5)
                    await expire_synthetic_lease(db, ident)
                    assert not await synthetic.consume_synthetic_send(marker)
                    results = await asyncio.gather(
                        *(
                            synthetic.classify_synthetic_unknown(
                                lease.owner, expired=True
                            )
                            for _ in range(3)
                        )
                    )
                    assert results.count(True) == 1
                    assert await store.claim(uuid4()) is None
                    assert await store.recover_next() is None
                    assert await adapter.counts(ident) == (0, 0)
                finally:
                    resume.set()
                    evidence = await sender
                assert evidence
                before = await snapshot(db, ident)
                assert not await synthetic.finalize_synthetic(evidence)
                assert await snapshot(db, ident) == before
                state, [attempt] = before
                assert (state["state"], state["status"], attempt["outcome"]) == (
                    "UNKNOWN",
                ) * 3
                assert state["lease_owner"] is None and not state["receipts"]
                assert attempt["send_marked_at"] == marker.marked_at
                assert await adapter.counts(ident) == (1, 1)
                # Inject duplicate adapter arrivals directly to test the independent ledger.
                outcomes = await asyncio.gather(
                    *(adapter.invoke(marker, effect=True) for _ in range(3))
                )
                assert all(outcome == evidence for outcome in outcomes)
                assert await adapter.counts(ident) == (4, 1)

    asyncio.run(check())


@pytest.mark.parametrize("release_first", [False, True])
def test_synthetic_marker_races_release(upgraded, release_first):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            ident = await ready()
            lease = await store.claim(uuid4())
            assert lease
            synthetic = ExecutionWorkerStore(
                db, timedelta(seconds=30), synthetic_key=urandom(32)
            )
            if release_first:
                await expire_synthetic_lease(db, ident)
                assert await synthetic.mark_synthetic_send(lease.owner) is None
                assert await store.recover_next()
                assert await synthetic.mark_synthetic_send(lease.owner) is None
                next_lease = await store.claim(lease.owner.worker_id)
                assert (
                    next_lease and next_lease.owner.generation > lease.owner.generation
                )
                assert await synthetic.mark_synthetic_send(lease.owner) is None
            else:
                marker, closed = await asyncio.gather(
                    synthetic.mark_synthetic_send(lease.owner),
                    store.release(lease.owner),
                )
                assert bool(marker) != bool(closed)
                state, [attempt] = await snapshot(db, ident)
                assert state["state"] == ("DISPATCHING" if marker else "READY")
                assert attempt["outcome"] == (None if marker else "NOT_SENT")

    asyncio.run(check())


@pytest.mark.parametrize(
    "sql,code",
    [
        ("DELETE FROM execution_dispatch_gate", "EXECUTION_COORDINATION_UNAVAILABLE"),
        (
            "DELETE FROM execution_authorization_guard",
            "EXECUTION_COORDINATION_UNAVAILABLE",
        ),
        (
            "UPDATE execution_dispatch_gate SET mode='LEGACY'",
            "DURABLE_ADMISSION_DISABLED",
        ),
        (
            "UPDATE execution_dispatch_gate SET mode='PAUSED'",
            "DURABLE_ADMISSION_DISABLED",
        ),
    ],
)
def test_synthetic_mutations_fail_closed(upgraded, sql, code):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, harness):
                first = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                evidence = await harness.send_if_current(marker, effect=False)
                assert evidence
                second = await ready()
                unmarked = await store.claim(uuid4())
                assert unmarked
                async with db.begin() as conn:
                    await execute(conn, sql)
                before = [await snapshot(db, item) for item in (first, second)]
                for method, arg in (
                    (synthetic.mark_synthetic_send, unmarked.owner),
                    (synthetic.consume_synthetic_send, marker),
                    (synthetic.finalize_synthetic, evidence),
                    (synthetic.classify_synthetic_unknown, marker.owner),
                ):
                    with pytest.raises(CommandStoreError, match=f"^{code}$"):
                        await method(arg)
                assert [await snapshot(db, item) for item in (first, second)] == before
                assert await adapter.counts(first) == (1, 0)
                assert await adapter.counts(second) == (0, 0)

    asyncio.run(check())


def test_synthetic_finalization_races_unknown_classification(upgraded):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, harness):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                evidence = await harness.send_if_current(marker, effect=True)
                assert evidence
                before, _ = await snapshot(db, ident)
                finalized, unknown = await asyncio.gather(
                    synthetic.finalize_synthetic(evidence),
                    synthetic.classify_synthetic_unknown(lease.owner),
                )
                assert finalized != unknown
                state, [attempt] = await snapshot(db, ident)
                expected = "SUCCEEDED" if finalized else "UNKNOWN"
                assert (state["state"], state["status"], attempt["outcome"]) == (
                    expected,
                ) * 3
                assert state["audit"] == before["audit"] + 1
                assert state["events"] == before["events"] + 1
                assert state["receipts"] == int(finalized)
                assert await adapter.counts(ident) == (1, 1)

    asyncio.run(check())


def test_synthetic_invocation_is_consumed_across_instances(upgraded):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, first):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                other_store = ExecutionWorkerStore(
                    db, timedelta(seconds=30), synthetic_key=adapter._key
                )
                second = SyntheticHarness(other_store, adapter)
                outcomes = await asyncio.gather(
                    first.send_if_current(marker, effect=True),
                    second.send_if_current(marker, effect=True),
                )
                [evidence] = [item for item in outcomes if item is not None]
                assert await adapter.counts(ident) == (1, 1)
                state, _ = await snapshot(db, ident)
                assert (
                    sum(
                        event["action"] == "SYNTHETIC_INVOCATION_CONSUMED"
                        for event in state["audit_rows"]
                    )
                    == 1
                )
                assert await synthetic.finalize_synthetic(evidence)

    asyncio.run(check())


@pytest.mark.parametrize("lost_ack", [False, True])
def test_synthetic_consumption_survives_crash(upgraded, monkeypatch, lost_ack):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, first):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                exit_transaction = AsyncTransaction.__aexit__

                async def lose_ack(transaction, *failure):
                    await exit_transaction(transaction, *failure)
                    raise OSError("lost invocation authorization acknowledgment")

                async def crash():
                    raise RuntimeError("crash before adapter invocation")

                with monkeypatch.context() as patch:
                    if lost_ack:
                        patch.setattr(AsyncTransaction, "__aexit__", lose_ack)
                        with pytest.raises(
                            CommandStoreError, match="^EXECUTION_OWNERSHIP_UNAVAILABLE$"
                        ):
                            await first.send_if_current(marker, effect=True)
                    else:
                        with pytest.raises(RuntimeError, match="crash before adapter"):
                            await first.send_if_current(
                                marker, effect=True, before_invoke=crash
                            )
                second = SyntheticHarness(synthetic, adapter)
                assert await second.send_if_current(marker, effect=True) is None
                assert await adapter.counts(ident) == (0, 0)
                await expire_synthetic_lease(db, ident)
                assert await synthetic.classify_synthetic_unknown(
                    lease.owner, expired=True
                )
                assert await store.claim(uuid4()) is None
                state, [attempt] = await snapshot(db, ident)
                assert state["state"] == attempt["outcome"] == "UNKNOWN"
                assert state["receipts"] == 0
                assert attempt["send_marked_at"] == marker.marked_at
                assert await adapter.counts(ident) == (0, 0)

    asyncio.run(check())


def test_synthetic_expired_finalizer_cannot_change_dispatching(upgraded):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, harness):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                evidence = await harness.send_if_current(marker, effect=True)
                assert evidence
                await expire_synthetic_lease(db, ident)
                before = await snapshot(db, ident)
                assert before[0]["state"] == "DISPATCHING"
                assert not await synthetic.finalize_synthetic(evidence)
                assert await snapshot(db, ident) == before
                assert await adapter.counts(ident) == (1, 1)
                assert await synthetic.classify_synthetic_unknown(
                    lease.owner, expired=True
                )

    asyncio.run(check())


def test_synthetic_conflicting_attestations_have_one_canonical_receipt(upgraded):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, harness):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                success = await harness.send_if_current(marker, effect=True)
                assert success
                # Model faulty adapter evidence. Both attestations authenticate.
                conflict = success.model_copy(update={"result": "DEFINITIVE_NO_EFFECT"})
                conflict = conflict.model_copy(
                    update={
                        "attestation": hmac_digest(
                            adapter._key, synthetic_evidence_bytes(conflict), "sha256"
                        )
                    }
                )
                before, _ = await snapshot(db, ident)
                outcomes = await asyncio.gather(
                    synthetic.finalize_synthetic(success),
                    synthetic.finalize_synthetic(conflict),
                )
                assert outcomes.count(True) == 1
                winning = success if outcomes[0] else conflict
                losing = conflict if outcomes[0] else success
                saved = await synthetic.read_synthetic_attempt(lease.owner, winning)
                rejected = await synthetic.read_synthetic_attempt(lease.owner, losing)
                assert saved and saved["receipt_matches"]
                assert rejected and not rejected["receipt_matches"]
                state, [attempt] = await snapshot(db, ident)
                assert state["state"] == state["status"] == attempt["outcome"]
                assert state["receipts"] == 1
                assert state["audit"] == before["audit"] + 1
                assert state["events"] == before["events"] + 1
                assert await adapter.counts(ident) == (1, 1)

    asyncio.run(check())


def test_synthetic_invocation_race_across_os_processes(upgraded):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, _):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                config = (
                    json.dumps(
                        {
                            "database_url": db.url.render_as_string(
                                hide_password=False
                            ),
                            "key": adapter._key.hex(),
                            "marker": marker.model_dump(mode="json"),
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
                            "import runpy, sys; runpy.run_path(sys.argv[1], run_name='__main__')",
                            str(
                                Path(__file__).with_name(
                                    "synthetic_execution_adapter.py"
                                )
                            ),
                            cwd=Path(__file__).resolve().parents[1],
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                        )
                        processes.append(child)
                        assert child.stdin and child.stdout
                        child.stdin.write(config)
                        await child.stdin.drain()
                        assert (
                            await asyncio.wait_for(child.stdout.readline(), 10)
                            == b"ready\n"
                        )
                    assert len({child.pid for child in processes}) == 2
                    results = await asyncio.wait_for(
                        asyncio.gather(
                            *(child.communicate(b"start\n") for child in processes)
                        ),
                        10,
                    )
                    assert all(child.returncode == 0 for child in processes)
                    assert sorted(output.strip() for output, _ in results) == [
                        b"denied",
                        b"permitted",
                    ]
                    assert await adapter.counts(ident) == (1, 1)
                finally:
                    for child in processes:
                        if child.returncode is None:
                            child.kill()
                            await child.wait()

    asyncio.run(check())


def test_synthetic_consumption_pre_commit_failure(upgraded, monkeypatch):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, first):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                before = await snapshot(db, ident)

                async def fail_before_commit(transaction, *failure):
                    await transaction.rollback()
                    raise OSError("pre-commit invocation authorization fault")

                with monkeypatch.context() as patch:
                    patch.setattr(AsyncTransaction, "__aexit__", fail_before_commit)
                    with pytest.raises(
                        CommandStoreError, match="^EXECUTION_OWNERSHIP_UNAVAILABLE$"
                    ):
                        await first.send_if_current(marker, effect=True)
                assert await snapshot(db, ident) == before
                assert await adapter.counts(ident) == (0, 0)
                # A distinct caller explicitly competes for the still-unconsumed authorization.
                second = SyntheticHarness(synthetic, adapter)
                assert await second.send_if_current(marker, effect=True)
                assert await first.send_if_current(marker, effect=True) is None
                assert await adapter.counts(ident) == (1, 1)

    asyncio.run(check())


@asynccontextmanager
async def synthetic_maintenance(db):
    role = "toolmesh_synthetic_maintenance_test"
    password = "synthetic-maintenance-test"
    async with db.begin() as conn:
        await execute(conn, f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        await execute(conn, f"GRANT CONNECT ON DATABASE toolmesh TO {role}")
        await execute(conn, f"GRANT USAGE ON SCHEMA public TO {role}")
        await execute(
            conn,
            f"GRANT SELECT, UPDATE, DELETE, TRUNCATE ON audit_events TO {role}",
        )
    maintenance = create_async_engine(
        db.url.set(username=role, password=password), hide_parameters=True
    )
    try:
        yield maintenance
    finally:
        await maintenance.dispose()
        async with db.begin() as conn:
            await execute(conn, f"DROP OWNED BY {role}")
            await execute(conn, f"DROP ROLE {role}")


def test_synthetic_consumption_is_database_protected(upgraded):
    async def check():
        async with ownership(upgraded) as (db, worker_db, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, first):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                assert await first.send_if_current(marker, effect=True)
                before = await snapshot(db, ident)
                mutations = (
                    "DELETE FROM audit_events WHERE resource_id=:id AND action='SYNTHETIC_INVOCATION_CONSUMED'",
                    "UPDATE audit_events SET action='RENAMED' WHERE resource_id=:id AND action='SYNTHETIC_INVOCATION_CONSUMED'",
                    "TRUNCATE audit_events",
                )
                async with synthetic_maintenance(db) as maintenance:
                    for engine in (worker_db, maintenance, db):
                        for query in mutations:
                            async with engine.connect() as conn:
                                with pytest.raises(DBAPIError) as error:
                                    await execute(
                                        conn,
                                        query,
                                        **({"id": ident} if ":id" in query else {}),
                                    )
                                assert error.value.orig.sqlstate == "42501"
                                if engine is not worker_db:
                                    assert (
                                        "synthetic consumption evidence is protected"
                                        in str(error.value.orig)
                                    )
                    assert await snapshot(db, ident) == before
                    assert (
                        await SyntheticHarness(synthetic, adapter).send_if_current(
                            marker, effect=True
                        )
                        is None
                    )
                    await expire_synthetic_lease(db, ident)
                    assert await synthetic.classify_synthetic_unknown(
                        lease.owner, expired=True
                    )
                    assert (
                        await SyntheticHarness(synthetic, adapter).send_if_current(
                            marker, effect=True
                        )
                        is None
                    )
                    assert await adapter.counts(ident) == (1, 1)

    asyncio.run(check())


def test_synthetic_consumption_rejects_missing_protection(upgraded):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            ident = await ready()
            lease = await store.claim(uuid4())
            assert lease
            synthetic = ExecutionWorkerStore(
                db, timedelta(seconds=30), synthetic_key=urandom(32)
            )
            marker = await synthetic.mark_synthetic_send(lease.owner)
            assert marker and marker.owner.execution_id == ident
            before = await snapshot(db, ident)
            with pytest.raises(
                CommandStoreError,
                match="^SYNTHETIC_CONSUMPTION_PROTECTION_UNAVAILABLE$",
            ):
                await synthetic.consume_synthetic_send(marker)
            assert await snapshot(db, ident) == before

    asyncio.run(check())


@pytest.mark.parametrize(
    "trigger",
    [
        "protect_synthetic_consumption_rows",
        "protect_synthetic_consumption_truncate",
    ],
)
def test_synthetic_consumption_rejects_disabled_guard(upgraded, trigger):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, harness):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                async with db.begin() as conn:
                    await execute(
                        conn, f"ALTER TABLE audit_events DISABLE TRIGGER {trigger}"
                    )
                before = await snapshot(db, ident)
                with pytest.raises(
                    CommandStoreError,
                    match="^SYNTHETIC_CONSUMPTION_PROTECTION_UNAVAILABLE$",
                ):
                    await harness.send_if_current(marker, effect=True)
                assert await snapshot(db, ident) == before
                assert await adapter.counts(ident) == (0, 0)

    asyncio.run(check())


def test_synthetic_reserved_audit_insert_and_role_boundaries(upgraded):
    async def check():
        async with ownership(upgraded) as (db, worker, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, harness):
                ident = await ready()
                lease = await store.claim(uuid4())
                assert lease
                marker = await synthetic.mark_synthetic_send(lease.owner)
                assert marker
                app = create_async_engine(
                    db.url.set(username="toolmesh_app", password="synthetic-app-test"),
                    hide_parameters=True,
                )
                try:
                    async with synthetic_maintenance(db) as maintenance:
                        for engine in (app, worker, maintenance):
                            async with engine.connect() as conn:
                                [privileges] = await rows(
                                    conn,
                                    """
                                    SELECT has_table_privilege(c.oid,'INSERT') AS can_insert,
                                        pg_has_role(c.relowner,'MEMBER') AS owner_member,
                                        pg_has_role(c.relowner,'USAGE') AS owner_inherited,
                                        pg_has_role(p.proowner,'MEMBER') AS function_owner_member,
                                        pg_has_role(p.proowner,'USAGE') AS function_owner_inherited,
                                        rol.rolsuper, rol.rolcreaterole, rol.rolreplication
                                    FROM pg_class c, pg_proc p, pg_roles rol
                                    WHERE c.oid='public.audit_events'::regclass
                                      AND p.oid='public.protect_synthetic_consumption()'::regprocedure
                                      AND rol.rolname=current_user
                                    """,
                                )
                                assert privileges.pop("can_insert") is (engine is app)
                                assert not any(privileges.values())
                                if engine is app:
                                    await execute(
                                        conn,
                                        """
                                        INSERT INTO audit_events(actor,action,resource_id)
                                        VALUES ('sandbox','ORDINARY_AUDIT',:id)
                                        """,
                                        id=ident,
                                    )
                            async with engine.connect() as conn:
                                with pytest.raises(DBAPIError) as rejected:
                                    await execute(
                                        conn,
                                        """
                                        INSERT INTO audit_events(actor,action,resource_id)
                                        VALUES ('sandbox','SYNTHETIC_INVOCATION_CONSUMED',:id)
                                        """,
                                        id=ident,
                                    )
                                assert rejected.value.orig.sqlstate == "42501"
                                if engine is app:
                                    assert (
                                        "synthetic consumption action is reserved"
                                        in str(rejected.value.orig)
                                    )
                            async with engine.connect() as conn:
                                with pytest.raises(DBAPIError) as rejected:
                                    await execute(
                                        conn,
                                        "ALTER TABLE audit_events DISABLE TRIGGER protect_synthetic_consumption_rows",
                                    )
                                assert rejected.value.orig.sqlstate == "42501"
                    assert await harness.send_if_current(marker, effect=True)
                    assert await adapter.counts(ident) == (1, 1)
                finally:
                    await app.dispose()

    asyncio.run(check())


def test_maintenance_races_in_flight_synthetic_consumption(upgraded, monkeypatch):
    async def check():
        async with ownership(upgraded) as (db, _, store, ready):
            async with synthetic_fixture(db) as (synthetic, adapter, harness):
                async with synthetic_maintenance(db) as maintenance:
                    ident = await ready()
                    lease = await store.claim(uuid4())
                    assert lease
                    marker = await synthetic.mark_synthetic_send(lease.owner)
                    assert marker
                    pending_commit = asyncio.Event()
                    release_commit = asyncio.Event()
                    original_exit = AsyncTransaction.__aexit__

                    async def pause_commit(transaction, *failure):
                        if asyncio.current_task() is consume:
                            pending_commit.set()
                            await release_commit.wait()
                        await original_exit(transaction, *failure)

                    with monkeypatch.context() as patch:
                        patch.setattr(AsyncTransaction, "__aexit__", pause_commit)
                        consume = asyncio.create_task(
                            synthetic.consume_synthetic_send(marker)
                        )
                        truncate = None
                        try:
                            await asyncio.wait_for(pending_commit.wait(), 5)
                            async with maintenance.connect() as conn:
                                # The uncommitted consumption event is invisible to row DML.
                                for statement in (
                                    "UPDATE audit_events SET action='RENAMED'",
                                    "DELETE FROM audit_events",
                                ):
                                    assert not await rows(
                                        conn,
                                        statement
                                        + """
                                        WHERE resource_id=:id AND action='SYNTHETIC_INVOCATION_CONSUMED'
                                        RETURNING id
                                        """,
                                        id=ident,
                                    )
                            async with maintenance.connect() as conn:
                                [backend] = await rows(
                                    conn, "SELECT pg_backend_pid() AS pid"
                                )
                                truncate = asyncio.create_task(
                                    execute(conn, "TRUNCATE audit_events")
                                )
                                async with asyncio.timeout(5), db.connect() as observer:
                                    while True:
                                        [state] = await rows(
                                            observer,
                                            "SELECT pg_blocking_pids(:pid) AS blockers",
                                            **backend,
                                        )
                                        if state["blockers"]:
                                            break
                                        await asyncio.sleep(0.01)
                                release_commit.set()
                                assert await consume
                                with pytest.raises(DBAPIError) as rejected:
                                    await truncate
                                assert rejected.value.orig.sqlstate == "42501"
                                assert (
                                    "synthetic consumption evidence is protected"
                                    in str(rejected.value.orig)
                                )
                        finally:
                            release_commit.set()
                            await asyncio.gather(consume, return_exceptions=True)
                            if truncate is not None and not truncate.done():
                                truncate.cancel()
                                await asyncio.gather(truncate, return_exceptions=True)
                    assert await harness.send_if_current(marker, effect=True) is None
                    assert await adapter.counts(ident) == (0, 0)
                    state, _ = await snapshot(db, ident)
                    assert (
                        sum(
                            event["action"] == "SYNTHETIC_INVOCATION_CONSUMED"
                            for event in state["audit_rows"]
                        )
                        == 1
                    )

    asyncio.run(check())
