"""Store integration tests on the repository's disposable PostgreSQL fixtures."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from os import urandom

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncTransaction, create_async_engine
from test_durable_execution_schema import IMAGE
from test_durable_execution_schema import sandbox as sandbox

from gateway.execution_contracts import ProtectedIntentScope, SealedPayload
from gateway.execution_crypto import (
    CommandDigests,
    PayloadContext,
    PayloadProtection,
    PayloadUnavailable,
    SandboxWrappingKeys,
)
from gateway.execution_store import CommandStore, CommandStoreError, CommandSubmission
from shared.database import execute, rows

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires disposable PostgreSQL image"
)
TOOL = "payments.issue_refund"
REQUEST = CommandSubmission(
    workspace_id="default",
    requester="finance-agent",
    tool_id=TOOL,
    arguments={
        "customer_id": "C-102",
        "transaction_id": "synthetic-private-transaction",
        "currency": "USD",
        "amount_minor": 123,
    },
    idempotency_key="synthetic-private-request-key",
)


@asynccontextmanager
async def ledger(sandbox):
    url, _, _ = sandbox
    db = create_async_engine(
        url.replace("postgresql:", "postgresql+asyncpg:"), hide_parameters=True
    )
    payloads = PayloadProtection(SandboxWrappingKeys({1: urandom(32)}, 1))
    digests = CommandDigests(urandom(32), urandom(32))
    try:
        async with db.begin() as conn:
            await execute(conn, "UPDATE execution_dispatch_gate SET mode='DURABLE'")
            await execute(
                conn,
                "UPDATE tools SET active_version=1, lifecycle='published' "
                "WHERE id IN ('payments.issue_refund','it.restart_service')",
            )
        yield db, CommandStore(db, payloads, digests), payloads, digests
    finally:
        async with db.begin() as conn:
            await execute(
                conn,
                "INSERT INTO execution_dispatch_gate VALUES (true,'LEGACY') "
                "ON CONFLICT (singleton) DO UPDATE SET mode='LEGACY'",
            )
            await execute(
                conn,
                "INSERT INTO execution_authorization_guard VALUES (true,1) ON CONFLICT DO NOTHING",
            )
            await execute(conn, "DELETE FROM execution_legacy_key_aliases")
            await execute(conn, "DELETE FROM execution_request_keys")
            await execute(conn, "DELETE FROM execution_commands")
            created = await rows(
                conn,
                "DELETE FROM approvals WHERE request_key LIKE 'm9-sealed:%' RETURNING id",
            )
            for item in created:
                await execute(
                    conn, "DELETE FROM audit_events WHERE resource_id=:id", **item
                )
                await execute(conn, "DELETE FROM executions WHERE id=:id", **item)
            await execute(
                conn, "UPDATE tools SET active_version=1 WHERE id=:tool", tool=TOOL
            )
            await execute(
                conn,
                "DELETE FROM tool_versions WHERE tool_id=:tool AND version=2",
                tool=TOOL,
            )
        await db.dispose()


async def assert_ledger_counts(db, expected):
    async with db.connect() as conn:
        counts = await rows(
            conn,
            """
            SELECT (SELECT count(*) FROM execution_commands) AS commands,
                   (SELECT count(*) FROM execution_request_keys) AS reservations,
                   (SELECT count(*) FROM approvals WHERE request_key LIKE 'm9-sealed:%') AS approvals,
                   (SELECT count(*) FROM executions e WHERE EXISTS (
                       SELECT 1 FROM approvals a WHERE a.id=e.id AND a.request_key LIKE 'm9-sealed:%'
                   )) AS executions,
                   (SELECT count(*) FROM audit_events WHERE action='DURABLE_APPROVAL_REQUESTED') AS audit,
                   (SELECT count(*) FROM execution_attempts) AS attempts,
                   (SELECT count(*) FROM execution_receipts) AS receipts
            """,
        )
    assert counts == [
        dict.fromkeys(
            ("commands", "reservations", "approvals", "executions", "audit"), expected
        )
        | {"attempts": 0, "receipts": 0}
    ]


def test_concurrent_retries_and_legacy_rows_contain_no_plaintext(sandbox, caplog):
    async def check():
        async with ledger(sandbox) as (db, store, payloads, _):
            bound_parameters = []

            @event.listens_for(db.sync_engine, "before_cursor_execute")
            def capture_parameters(conn, cursor, statement, parameters, context, many):
                bound_parameters.append(parameters)

            # Each invocation owns a connection and transaction. A barrier
            # releases all callers together into the connection pool.
            barrier = asyncio.Barrier(24)

            async def submit():
                await barrier.wait()
                return await store.submit(REQUEST)

            async with asyncio.TaskGroup() as group:
                submissions = [group.create_task(submit()) for _ in range(24)]
            results = [task.result() for task in submissions]
            assert len({result.execution_id for result in results}) == 1
            assert sum(not result.replayed for result in results) == 1
            assert all(result.status == "PENDING_APPROVAL" for result in results)
            uid = results[0].execution_id
            await assert_ledger_counts(db, 1)
            async with db.connect() as conn:
                public = await rows(
                    conn,
                    """
                    SELECT a.arguments, to_jsonb(a)::text AS approval, to_jsonb(e)::text AS execution,
                           to_jsonb(ev)::text AS audit, to_jsonb(s)::text AS status_event
                    FROM approvals a JOIN executions e ON e.id=a.id
                    JOIN audit_events ev ON ev.resource_id=a.id
                    JOIN execution_events s ON s.execution_id=a.id
                    WHERE a.id=:id
                    """,
                    id=uid,
                )
                assert len(public) == 1 and public[0]["arguments"] == {}
                [encrypted] = await rows(
                    conn,
                    "SELECT ciphertext, wrapped_data_key, nonce, key_version "
                    "FROM execution_commands WHERE execution_id=:id",
                    id=uid,
                )
            binding = PayloadContext(
                "command",
                uid,
                ProtectedIntentScope(
                    workspace_id="default",
                    requester=REQUEST.requester,
                    customer_id="C-102",
                    tool_id=TOOL,
                ),
            )
            assert (
                json.loads(payloads.open(SealedPayload(**encrypted), binding))
                == REQUEST.arguments
            )
            for marker in ("synthetic-private-transaction", REQUEST.idempotency_key):
                assert marker not in str(public)
                assert marker.encode() not in encrypted["ciphertext"]
                assert marker not in repr(bound_parameters)
                assert marker not in repr(REQUEST)
                assert marker not in caplog.text

    asyncio.run(check())


def test_pinned_version_projection_and_intent_conflicts(sandbox):
    async def check():
        async with ledger(sandbox) as (db, store, _, _):
            first = await store.submit(REQUEST)
            async with db.begin() as conn:
                await execute(
                    conn,
                    "INSERT INTO tool_versions (tool_id, version, contract) "
                    "SELECT tool_id, 2, jsonb_set(contract, '{input_schema,properties,currency,pattern}', "
                    "'\"^EUR$\"'::jsonb) FROM tool_versions WHERE tool_id=:tool AND version=1",
                    tool=TOOL,
                )
                await execute(
                    conn, "UPDATE tools SET active_version=2 WHERE id=:tool", tool=TOOL
                )
            changed_projection = replace(
                REQUEST,
                fields=("id",),
                arguments=dict(reversed(list(REQUEST.arguments.items()))),
            )
            replay = await store.submit(changed_projection)
            assert replay.execution_id == first.execution_id
            assert replay.replayed and replay.version == 1
            for request in (
                replace(REQUEST, arguments=REQUEST.arguments | {"amount_minor": 124}),
                replace(REQUEST, version=2),
            ):
                with pytest.raises(CommandStoreError, match="^IDEMPOTENCY_CONFLICT$"):
                    await store.submit(request)
            with pytest.raises(CommandStoreError, match="^INVALID_ARGUMENT$"):
                await store.submit(replace(REQUEST, fields=("unknown-field",)))
            with pytest.raises(CommandStoreError, match="^INVALID_ARGUMENT$"):
                await store.submit(replace(REQUEST, idempotency_key="new-key"))
            second = await store.submit(
                replace(
                    REQUEST,
                    arguments=REQUEST.arguments | {"currency": "EUR"},
                    idempotency_key="new-key",
                )
            )
            assert second.version == 2 and second.execution_id != first.execution_id
            await assert_ledger_counts(db, 2)

    asyncio.run(check())


@pytest.mark.parametrize(
    "setting,expected",
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
def test_admission_fails_closed(sandbox, setting, expected):
    async def check():
        async with ledger(sandbox) as (db, store, _, _):
            async with db.begin() as conn:
                await execute(conn, setting)
            with pytest.raises(CommandStoreError, match=f"^{expected}$"):
                await store.submit(REQUEST)
            await assert_ledger_counts(db, 0)

    asyncio.run(check())


def test_legacy_key_cannot_create_duplicate_and_alias_resolves(sandbox):
    async def check():
        async with ledger(sandbox) as (db, store, _, digests):
            with pytest.raises(
                CommandStoreError, match="^LEGACY_RESERVATION_UNAVAILABLE$"
            ):
                await store.submit(replace(REQUEST, idempotency_key="legacy-one"))
            await assert_ledger_counts(db, 0)
            first = await store.submit(REQUEST)
            async with db.begin() as conn:
                await execute(
                    conn,
                    "UPDATE execution_request_keys SET key_digest=:different WHERE execution_id=:id",
                    different=urandom(32),
                    id=first.execution_id,
                )
                await execute(
                    conn,
                    "INSERT INTO execution_legacy_key_aliases (requester, tool_id, key_digest, execution_id) "
                    "VALUES (:requester,:tool,:key,:id)",
                    requester=REQUEST.requester,
                    tool=TOOL,
                    id=first.execution_id,
                    key=digests.legacy_alias(
                        REQUEST.requester, TOOL, REQUEST.idempotency_key
                    ),
                )
            replay = await store.submit(REQUEST)
            assert replay.execution_id == first.execution_id and replay.replayed
            await assert_ledger_counts(db, 1)

    asyncio.run(check())


def test_denied_or_changed_input_cannot_write_records(sandbox):
    async def check():
        async with ledger(sandbox) as (db, store, _, _):
            for request, error in (
                (replace(REQUEST, requester="missing"), "TOOL_NOT_AVAILABLE"),
                (replace(REQUEST, requester="support-agent"), "TOOL_NOT_AVAILABLE"),
                (
                    replace(
                        REQUEST, arguments=REQUEST.arguments | {"customer_id": "C-103"}
                    ),
                    "TOOL_NOT_AVAILABLE",
                ),
                (
                    replace(
                        REQUEST, arguments=REQUEST.arguments | {"amount_minor": 123.0}
                    ),
                    "INVALID_ARGUMENT",
                ),
                (replace(REQUEST, idempotency_key=""), "IDEMPOTENCY_KEY_REQUIRED"),
            ):
                with pytest.raises(CommandStoreError, match=f"^{error}$"):
                    await store.submit(request)
            accepted = await store.submit(
                replace(
                    REQUEST,
                    tool_id="it.restart_service",
                    requester="it-agent",
                    arguments={
                        "customer_id": "C-102",
                        "service_id": "synthetic-service",
                    },
                )
            )
            assert accepted.version == 1
            await assert_ledger_counts(db, 1)

    asyncio.run(check())


@pytest.mark.parametrize("retryable", [False, True], ids=["rollback", "transient-lock"])
def test_database_fault_rolls_back_before_retry(sandbox, retryable):
    async def check():
        async with ledger(sandbox) as (db, _, payloads, digests):
            store = CommandStore(
                db.execution_options(isolation_level="AUTOCOMMIT"), payloads, digests
            )
            async with db.begin() as conn:
                before = await rows(
                    conn,
                    "SELECT (SELECT count(*) FROM executions) AS executions, "
                    "(SELECT count(*) FROM execution_events) AS status_events",
                )
                # Sequence increments survive transaction rollback, making the
                # first-attempt failure deterministic and observable.
                await execute(conn, "CREATE SEQUENCE synthetic_audit_attempt")
                failure = (
                    "IF nextval('synthetic_audit_attempt') = 1 THEN "
                    "RAISE EXCEPTION 'synthetic lock failure' USING ERRCODE = '55P03'; END IF;"
                    if retryable
                    else "RAISE EXCEPTION 'synthetic failure';"
                )
                await execute(
                    conn,
                    f"""
                    CREATE FUNCTION reject_synthetic_audit() RETURNS trigger
                    LANGUAGE plpgsql AS $$
                    BEGIN
                        {failure}
                        RETURN NEW;
                    END $$
                    """,
                )
                await execute(
                    conn,
                    """
                    CREATE TRIGGER reject_synthetic_audit BEFORE INSERT ON audit_events
                    FOR EACH ROW WHEN (NEW.action = 'DURABLE_APPROVAL_REQUESTED')
                    EXECUTE FUNCTION reject_synthetic_audit()
                    """,
                )
            try:
                if retryable:
                    accepted = await store.submit(REQUEST)
                    assert not accepted.replayed
                    await assert_ledger_counts(db, 1)
                    async with db.connect() as conn:
                        assert (
                            await conn.scalar(
                                text("SELECT last_value FROM synthetic_audit_attempt")
                            )
                            == 2
                        )
                else:
                    with pytest.raises(
                        CommandStoreError, match="^COMMAND_STORE_UNAVAILABLE$"
                    ):
                        await store.submit(REQUEST)
                    await assert_ledger_counts(db, 0)
                    async with db.connect() as conn:
                        assert (
                            await rows(
                                conn,
                                "SELECT (SELECT count(*) FROM executions) AS executions, "
                                "(SELECT count(*) FROM execution_events) AS status_events",
                            )
                            == before
                        )
            finally:
                async with db.begin() as conn:
                    await execute(
                        conn, "DROP TRIGGER reject_synthetic_audit ON audit_events"
                    )
                    await execute(conn, "DROP FUNCTION reject_synthetic_audit()")
                    await execute(conn, "DROP SEQUENCE synthetic_audit_attempt")

    asyncio.run(check())


@pytest.mark.parametrize("committed", [True, False], ids=["lost-ack", "no-commit"])
def test_commit_ambiguity_recovers_without_reissuing_write(
    sandbox, monkeypatch, committed
):
    async def check():
        async with ledger(sandbox) as (db, store, _, _):
            commit = AsyncTransaction.commit

            async def lose_ack(transaction):
                if committed:
                    await commit(transaction)
                raise OSError("synthetic lost database acknowledgment")

            with monkeypatch.context() as patch:
                patch.setattr(AsyncTransaction, "commit", lose_ack)
                if committed:
                    recovered = await store.submit(REQUEST)
                    assert recovered.replayed and recovered.status == "PENDING_APPROVAL"
                else:
                    with pytest.raises(
                        CommandStoreError, match="^ADMISSION_COMMIT_UNCONFIRMED$"
                    ):
                        await store.submit(REQUEST)
            await assert_ledger_counts(db, int(committed))
            result = await store.submit(REQUEST)
            await assert_ledger_counts(db, 1)
            if committed:
                assert result.execution_id == recovered.execution_id
            else:
                assert not result.replayed

    asyncio.run(check())


def test_unavailable_key_rolls_back_admission(sandbox):
    class UnavailableProvider:
        def wrap(self, data_key, context):
            raise PayloadUnavailable("EXECUTION_PAYLOAD_UNAVAILABLE")

        def unwrap(self, version, wrapped_key, context):
            raise AssertionError("unwrap must not run")

    async def check():
        async with ledger(sandbox) as (db, _, _, digests):
            store = CommandStore(db, PayloadProtection(UnavailableProvider()), digests)
            with pytest.raises(
                CommandStoreError, match="^EXECUTION_PAYLOAD_UNAVAILABLE$"
            ):
                await store.submit(REQUEST)
            await assert_ledger_counts(db, 0)

    asyncio.run(check())


def test_conflicting_legacy_alias_cannot_choose_an_effect(sandbox):
    async def check():
        async with ledger(sandbox) as (db, store, _, digests):
            first = await store.submit(REQUEST)
            second = await store.submit(replace(REQUEST, idempotency_key="another-key"))
            assert first.execution_id != second.execution_id
            async with db.begin() as conn:
                await execute(
                    conn,
                    "INSERT INTO execution_legacy_key_aliases (requester, tool_id, key_digest, execution_id) "
                    "VALUES (:requester,:tool,:key,:id)",
                    requester=REQUEST.requester,
                    tool=TOOL,
                    id=second.execution_id,
                    key=digests.legacy_alias(
                        REQUEST.requester, TOOL, REQUEST.idempotency_key
                    ),
                )
            with pytest.raises(CommandStoreError, match="^IDEMPOTENCY_CONFLICT$"):
                await store.submit(REQUEST)
            await assert_ledger_counts(db, 2)

    asyncio.run(check())


def test_admission_racing_guard_removal_fails_closed(sandbox):
    async def check():
        async with ledger(sandbox) as (db, store, _, _):
            reading_guard = asyncio.Event()

            @event.listens_for(db.sync_engine, "before_cursor_execute")
            def mark_guard_read(conn, cursor, statement, parameters, context, many):
                if (
                    "FROM execution_authorization_guard WHERE singleton FOR SHARE"
                    in statement
                ):
                    reading_guard.set()

            async with asyncio.TaskGroup() as group:

                async def submit():
                    with pytest.raises(
                        CommandStoreError, match="^EXECUTION_COORDINATION_UNAVAILABLE$"
                    ):
                        await store.submit(REQUEST)

                async with db.begin() as conn:
                    await execute(conn, "DELETE FROM execution_authorization_guard")
                    pending = group.create_task(submit())
                    await asyncio.wait_for(reading_guard.wait(), timeout=1)
                    assert not pending.done()
            await assert_ledger_counts(db, 0)

    asyncio.run(check())


def test_replay_rechecks_current_policy(sandbox):
    async def check():
        async with ledger(sandbox) as (db, store, _, _):
            await store.submit(REQUEST)
            async with db.begin() as conn:
                await execute(
                    conn,
                    "UPDATE refund_grants SET enabled=false WHERE principal='finance-agent'",
                )
            try:
                with pytest.raises(CommandStoreError, match="^TOOL_NOT_AVAILABLE$"):
                    await store.submit(REQUEST)
                await assert_ledger_counts(db, 1)
            finally:
                async with db.begin() as conn:
                    await execute(
                        conn,
                        "UPDATE refund_grants SET enabled=true WHERE principal='finance-agent'",
                    )

    asyncio.run(check())
