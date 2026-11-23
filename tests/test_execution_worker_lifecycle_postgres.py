"""Standalone worker exercised using dedicated logins and disposable PostgreSQL."""

import asyncio
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_durable_execution_schema import IMAGE, docker, migrate
from test_durable_execution_schema import sandbox as sandbox
from test_execution_store import REQUEST, ledger
from test_execution_worker_postgres import snapshot

from gateway.execution_approval import ExecutionApprovals
from gateway.execution_contracts import ExecutionOwner, ExecutionState
from gateway.execution_store import CommandStoreError
from gateway.sandbox_worker_store import SandboxWorkerStore
from shared.database import execute, rows

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires disposable PostgreSQL image"
)
PROVISION = Path(__file__).with_name("fixtures") / "m914_worker_privileges.sql"
LOGINS = ("m914_worker_a", "m914_worker_b")
PASSWORD = "disposable-ownership-worker"


@pytest.fixture(scope="module")
def upgraded(sandbox):
    _, env, _ = sandbox
    migrate(env, "upgrade", "0011")
    return sandbox


@asynccontextmanager
async def restricted(upgraded):
    async with ledger(upgraded) as (db, commands, payloads, digests):
        async with db.begin() as conn:
            await execute(conn, "SELECT 1")
            driver = (await conn.get_raw_connection()).driver_connection
            await driver.execute(PROVISION.read_text())
            for login in LOGINS:
                await execute(
                    conn,
                    f"CREATE ROLE {login} LOGIN PASSWORD '{PASSWORD}' IN ROLE m914_worker_access",
                )
                await execute(conn, f"GRANT CONNECT ON DATABASE toolmesh TO {login}")
        engines = [
            create_async_engine(
                db.url.set(username=login, password=PASSWORD), hide_parameters=True
            )
            for login in LOGINS
        ]
        approvals = ExecutionApprovals(db, payloads, digests, timedelta(minutes=3))

        async def ready():
            command = await commands.submit(
                replace(REQUEST, idempotency_key=str(uuid4()))
            )
            await approvals.decide(
                command.execution_id, "refund-approver", "APPROVED", "sandbox ownership"
            )
            return command.execution_id

        try:
            yield db, engines, ready, approvals
        finally:
            for engine in engines:
                await engine.dispose()
            async with db.begin() as conn:
                for table in (
                    "execution_receipts",
                    "execution_attempts",
                    "execution_approval_decisions",
                ):
                    await execute(conn, f"DELETE FROM {table}")
                await execute(conn, "DROP SCHEMA m914_ownership CASCADE")
                for login in (*LOGINS, "m914_worker_access", "m914_function_owner"):
                    await execute(conn, f"DROP OWNED BY {login}")
                    await execute(conn, f"DROP ROLE {login}")


def test_restricted_schema_fence_and_effective_privileges(upgraded):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            ident = await ready()
            first, second = [
                SandboxWorkerStore(engine, timedelta(seconds=5)) for engine in engines
            ]
            await first.validate_schema()
            worker, other = await first.register(), await second.register()
            assert worker != other
            before = await snapshot(db, ident)
            async with engines[0].connect() as conn:
                [privileges] = await rows(
                    conn,
                    """
                    SELECT current_user AS login, rol.rolsuper OR rol.rolcreaterole
                         OR rol.rolcreatedb OR rol.rolreplication OR rol.rolbypassrls AS elevated,
                         pg_has_role('m914_function_owner','MEMBER') AS owner_member,
                         pg_has_role('m914_function_owner','USAGE') AS owner_inherited,
                         pg_has_role('toolmesh_app','MEMBER') AS gateway_member,
                         has_schema_privilege('m914_ownership','CREATE') AS schema_create,
                         has_any_column_privilege('public.executions','UPDATE') AS execution_update,
                         has_any_column_privilege('public.execution_commands','UPDATE') AS command_update,
                         has_any_column_privilege('public.execution_attempts','INSERT,UPDATE') AS attempt_write,
                         has_table_privilege('public.audit_events','INSERT') AS audit_insert,
                         has_function_privilege('m914_ownership.operate(text,uuid,uuid,bigint,integer)','EXECUTE')
                             AS can_operate
                    FROM pg_roles rol WHERE rol.rolname=current_user
                    """,
                )
                assert privileges.pop("login") == LOGINS[0]
                assert privileges.pop("can_operate")
                assert not any(privileges.values())
            async with db.connect() as conn:
                functions = await rows(
                    conn,
                    """
                    SELECT p.prosecdef, p.proconfig,
                           r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole
                               OR r.rolcreatedb OR r.rolreplication AS unsafe_owner,
                           EXISTS (SELECT 1 FROM aclexplode(p.proacl) a
                                   WHERE a.grantee=0 AND a.privilege_type='EXECUTE') AS public_exec
                    FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
                    JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='m914_ownership'
                    """,
                )
                assert len(functions) == 2
                assert all(
                    f
                    == {
                        "prosecdef": True,
                        "proconfig": ["search_path=pg_catalog, pg_temp"],
                        "unsafe_owner": False,
                        "public_exec": False,
                    }
                    for f in functions
                )
            for statement in (
                "UPDATE public.executions SET status='FAILED'",
                "UPDATE public.execution_commands SET state='UNKNOWN'",
                "UPDATE public.execution_attempts SET send_marked_at=now()",
                "INSERT INTO public.audit_events(actor,action,resource_id) VALUES ('test','SYNTHETIC_INVOCATION_CONSUMED',gen_random_uuid())",
                "SET ROLE m914_function_owner",
                "CREATE TABLE m914_ownership.unauthorized (id integer)",
                "ALTER TABLE public.audit_events DISABLE TRIGGER ALL",
            ):
                async with engines[0].connect() as conn:
                    with pytest.raises(DBAPIError) as denied:
                        await execute(conn, statement)
                    assert denied.value.orig.sqlstate == "42501"
            assert await snapshot(db, ident) == before
            lease = await first.claim(worker)
            assert lease and lease.owner.execution_id == ident
            claimed = await snapshot(db, ident)
            assert (
                await second.release(
                    ExecutionOwner(
                        execution_id=ident,
                        generation=lease.owner.generation,
                        worker_id=other,
                    )
                )
                is None
            )
            with pytest.raises(CommandStoreError, match="^WORKER_ACCESS_DENIED$"):
                await second.release(lease.owner)
            assert await snapshot(db, ident) == claimed
            for revision in ("0010", "0012", None, "multiple", "missing"):
                async with db.begin() as conn:
                    if revision == "missing":
                        await execute(
                            conn,
                            "ALTER TABLE public.alembic_version RENAME TO hidden_version",
                        )
                    else:
                        await execute(conn, "DELETE FROM public.alembic_version")
                        for item in (
                            ("0011", "0012") if revision == "multiple" else (revision,)
                        ):
                            if item:
                                await execute(
                                    conn,
                                    "INSERT INTO public.alembic_version VALUES (:revision)",
                                    revision=item,
                                )
                try:
                    with pytest.raises(
                        CommandStoreError,
                        match="WORKER_SCHEMA_(UNSUPPORTED|UNAVAILABLE)",
                    ):
                        await first.validate_schema()
                    with pytest.raises(CommandStoreError):
                        await first.release(lease.owner)
                    assert await snapshot(db, ident) == claimed
                finally:
                    async with db.begin() as conn:
                        if revision == "missing":
                            await execute(
                                conn,
                                "ALTER TABLE public.hidden_version RENAME TO alembic_version",
                            )
                        else:
                            await execute(conn, "DELETE FROM public.alembic_version")
                            await execute(
                                conn,
                                "INSERT INTO public.alembic_version VALUES ('0011')",
                            )
            assert await first.release(lease.owner)

    asyncio.run(check())


async def start_worker(engine, *, program=None, **settings):
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "TOOLMESH_SANDBOX_WORKER_ENABLED": "true",
        "TOOLMESH_SANDBOX_WORKER_DATABASE_URL": engine.url.render_as_string(
            hide_password=False
        ),
        "TOOLMESH_SANDBOX_WORKER_LEASE_SECONDS": "1.5",
        "TOOLMESH_SANDBOX_WORKER_RENEW_SECONDS": "0.2",
        **{
            f"TOOLMESH_SANDBOX_WORKER_{key.upper()}": str(value)
            for key, value in settings.items()
        },
    }
    return await asyncio.create_subprocess_exec(
        sys.executable,
        *(("-m", "gateway.execution_worker") if program is None else ("-c", *program)),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def event_from(process, expected):
    async with asyncio.timeout(10):
        while line := await process.stdout.readline():
            if expected in line:
                return line
    raise AssertionError(f"worker exited before {expected!r}")


async def stop_worker(process, sig=signal.SIGTERM):
    if process.returncode is None:
        process.send_signal(sig)
    return await asyncio.wait_for(process.wait(), timeout=8)


def test_worker_process_contention_shutdown_and_expired_recovery(upgraded):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, approvals):
            ident = await ready()
            processes = []
            try:
                for engine in engines:
                    processes.append(await start_worker(engine))
                await asyncio.gather(
                    *(event_from(p, b"worker_ready") for p in processes)
                )

                async def winner():
                    async with asyncio.timeout(10):
                        while True:
                            state, attempts = await snapshot(db, ident)
                            if state["state"] == "CLAIMED":
                                return state, attempts
                            await asyncio.sleep(0.03)

                state, attempts = await winner()
                assert state["generation"] == len(attempts) == 1
                async with db.connect() as conn:
                    [who] = await rows(
                        conn,
                        "SELECT login FROM m914_ownership.sessions WHERE worker_id=:id",
                        id=state["lease_owner"],
                    )
                first = LOGINS.index(who["login"])
                await event_from(processes[first], b"ownership_renewed")
                replay = await approvals.decide(
                    ident, "refund-approver", "APPROVED", "sandbox ownership"
                )
                assert (
                    replay.replayed and replay.execution_state is ExecutionState.CLAIMED
                )
                assert (
                    await stop_worker(processes[first], signal.SIGKILL)
                    == -signal.SIGKILL
                )
                remaining = processes[1 - first]
                await event_from(remaining, b"ownership_claimed")
                state, attempts = await snapshot(db, ident)
                assert state["generation"] == len(attempts) == 2
                assert attempts[0]["outcome"] == "NOT_SENT"
                assert await stop_worker(remaining) == 0
                state, attempts = await snapshot(db, ident)
                assert state["state"] == "READY" and state["lease_owner"] is None
                assert state["receipts"] == 0
                assert all(
                    a["outcome"] == "NOT_SENT" and a["send_marked_at"] is None
                    for a in attempts
                )
            finally:
                for process in processes:
                    await stop_worker(process, signal.SIGKILL)

    asyncio.run(check())


@pytest.mark.parametrize("mode", ["LEGACY", "PAUSED", "missing_gate", "missing_guard"])
def test_restricted_coordination_failures_do_not_write(upgraded, mode):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            ident = await ready()
            store = SandboxWorkerStore(engines[0], timedelta(seconds=5))
            worker = await store.register()
            lease = await store.claim(worker)
            assert lease
            before = await snapshot(db, ident)
            async with db.begin() as conn:
                if mode == "missing_gate":
                    await execute(conn, "DELETE FROM execution_dispatch_gate")
                elif mode == "missing_guard":
                    await execute(conn, "DELETE FROM execution_authorization_guard")
                else:
                    await execute(
                        conn, "UPDATE execution_dispatch_gate SET mode=:mode", mode=mode
                    )
            for operation, arg in (
                (store.claim, worker),
                (store.recover_next, worker),
                (store.renew, lease.owner),
                (store.release, lease.owner),
            ):
                with pytest.raises(
                    CommandStoreError,
                    match=(
                        "EXECUTION_COORDINATION_UNAVAILABLE"
                        if mode.startswith("missing")
                        else "DURABLE_ADMISSION_DISABLED"
                    ),
                ):
                    await operation(arg)
            assert await snapshot(db, ident) == before

    asyncio.run(check())


def test_restricted_recovery_state_matrix(upgraded):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, approvals):
            store = SandboxWorkerStore(engines[0], timedelta(seconds=5))
            worker = await store.register()
            for phase in ("DISPATCHING", "MARKED_CLAIMED", "UNKNOWN", "FAILED"):
                ident = await ready()
                lease = await store.claim(worker)
                assert lease and lease.owner.execution_id == ident
                async with db.begin() as conn:
                    # Seed historical evidence as the fixture owner. The worker has no marker write path.
                    marked = phase != "FAILED"
                    if marked:
                        await execute(
                            conn,
                            """
                            UPDATE execution_attempts SET send_marked_at=claimed_at, admission_revision=1
                            WHERE execution_id=:id
                            """,
                            id=ident,
                        )
                    await execute(
                        conn,
                        """
                        UPDATE execution_commands SET state=:phase,
                            lease_owner=CASE WHEN :owned THEN lease_owner END,
                            lease_expires_at=CASE WHEN :owned THEN clock_timestamp()-interval '1 second' END
                        WHERE execution_id=:id
                        """,
                        id=ident,
                        phase="CLAIMED" if phase == "MARKED_CLAIMED" else phase,
                        owned=phase in ("DISPATCHING", "MARKED_CLAIMED"),
                    )
                    if phase in ("FAILED", "UNKNOWN"):
                        await execute(
                            conn,
                            "UPDATE executions SET status=:phase WHERE id=:id",
                            phase=phase,
                            id=ident,
                        )
                        await execute(
                            conn,
                            """
                            UPDATE execution_attempts SET finished_at=clock_timestamp(), outcome=:outcome
                            WHERE execution_id=:id
                            """,
                            outcome="UNKNOWN" if marked else "NOT_SENT",
                            id=ident,
                        )
                before = await snapshot(db, ident)
                assert before[0]["receipts"] == 0
                assert await store.recover_next(worker) is None
                assert await store.claim(worker) is None
                assert await store.release(lease.owner) is None
                assert await snapshot(db, ident) == before
            for claimed in (False, True):
                ident = await ready()
                if claimed:
                    lease = await store.claim(worker)
                    assert lease and lease.owner.execution_id == ident
                    assert await store.recover_next(worker) is None
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        UPDATE execution_commands SET dispatch_not_after=clock_timestamp()-interval '1 second',
                          lease_expires_at=CASE WHEN lease_owner IS NOT NULL THEN clock_timestamp()-interval '1 second' END
                        WHERE execution_id=:id
                        """,
                        id=ident,
                    )
                closed = await store.recover_next(worker)
                assert (
                    closed
                    and closed.execution_id == ident
                    and closed.state is ExecutionState.FAILED
                )
                replay = await approvals.decide(
                    ident, "refund-approver", "APPROVED", "sandbox ownership"
                )
                assert (
                    replay.replayed and replay.execution_state is ExecutionState.FAILED
                )
                assert await store.recover_next(worker) is None

    asyncio.run(check())


@pytest.fixture(scope="module", params=["fresh", "upgrade"])
def old_schema(request):
    yield from sandbox.__wrapped__(request)


def test_worker_process_rejects_actual_0010(old_schema):
    async def check():
        url, _, _ = old_schema
        db = create_async_engine(
            url.replace("postgresql:", "postgresql+asyncpg:"), hide_parameters=True
        )
        async with db.begin() as conn:
            await execute(conn, f"CREATE ROLE {LOGINS[0]} LOGIN PASSWORD '{PASSWORD}'")
            await execute(conn, f"GRANT CONNECT ON DATABASE toolmesh TO {LOGINS[0]}")
            await execute(conn, f"GRANT USAGE ON SCHEMA public TO {LOGINS[0]}")
            await execute(
                conn, f"GRANT SELECT ON public.alembic_version TO {LOGINS[0]}"
            )
        worker = create_async_engine(
            db.url.set(username=LOGINS[0], password=PASSWORD), hide_parameters=True
        )
        try:
            process = await start_worker(worker)
            output, _ = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 2 and b"WORKER_SCHEMA_UNSUPPORTED" in output
            assert b"worker_ready" not in output
            async with db.connect() as conn:
                assert await rows(conn, "SELECT version_num FROM alembic_version") == [
                    {"version_num": "0010"}
                ]
                assert await rows(
                    conn, "SELECT count(*) AS count FROM execution_attempts"
                ) == [{"count": 0}]
        finally:
            await worker.dispose()
            async with db.begin() as conn:
                await execute(conn, f"DROP OWNED BY {LOGINS[0]}")
                await execute(conn, f"DROP ROLE {LOGINS[0]}")
            await db.dispose()

    asyncio.run(check())


async def wait_for_database_block(db, blocker):
    async with asyncio.timeout(5), db.connect() as observer:
        while True:
            waiting = await rows(
                observer,
                """
                SELECT pid FROM pg_catalog.pg_stat_activity
                WHERE :blocker = ANY(pg_catalog.pg_blocking_pids(pid))
                """,
                blocker=blocker,
            )
            if waiting:
                return waiting[0]["pid"]
            await asyncio.sleep(0.01)


def test_schema_fence_rejects_stale_snapshot_and_revision_change_during_lock(upgraded):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            ident = await ready()
            store = SandboxWorkerStore(engines[0], timedelta(seconds=5))
            worker = await store.register()
            before = await snapshot(db, ident)
            try:
                async with engines[0].connect() as stale:
                    await stale.execution_options(isolation_level="REPEATABLE READ")
                    async with stale.begin():
                        assert await rows(
                            stale, "SELECT version_num FROM public.alembic_version"
                        ) == [{"version_num": "0011"}]
                        async with db.begin() as provisioner:
                            await execute(
                                provisioner,
                                "UPDATE alembic_version SET version_num='0012'",
                            )
                        for statement in (
                            "SELECT m914_ownership.register_worker()",
                            "SELECT * FROM m914_ownership.operate('claim',:worker,NULL,NULL,5000)",
                        ):
                            async with stale.begin_nested():
                                with pytest.raises(DBAPIError) as rejected:
                                    async with stale.begin_nested():
                                        await rows(stale, statement, worker=worker)
                                assert rejected.value.orig.sqlstate == "M9145"
                async with db.begin() as conn:
                    await execute(conn, "UPDATE alembic_version SET version_num='0011'")
                async with db.begin() as blocker:
                    [pid] = await rows(blocker, "SELECT pg_backend_pid() AS pid")
                    await rows(
                        blocker,
                        "SELECT singleton FROM execution_dispatch_gate FOR UPDATE",
                    )
                    claim = asyncio.create_task(store.claim(worker))
                    try:
                        await wait_for_database_block(db, pid["pid"])
                        await execute(
                            blocker, "UPDATE alembic_version SET version_num='0012'"
                        )
                    except BaseException:
                        claim.cancel()
                        await asyncio.gather(claim, return_exceptions=True)
                        raise
                with pytest.raises(
                    CommandStoreError, match="^WORKER_SCHEMA_UNSUPPORTED$"
                ):
                    await claim
                assert await snapshot(db, ident) == before
            finally:
                async with db.begin() as conn:
                    await execute(conn, "UPDATE alembic_version SET version_num='0011'")

            # The client overrides an engine configured with a stronger snapshot.
            rr = create_async_engine(
                engines[0].url, hide_parameters=True, isolation_level="REPEATABLE READ"
            )
            try:
                override = SandboxWorkerStore(rr, timedelta(seconds=5))
                await override.validate_schema()
                lease = await override.claim(worker)
                assert lease and lease.owner.execution_id == ident
            finally:
                await rr.dispose()

    asyncio.run(check())


def test_concurrent_claims_respect_one_slot_per_registration(upgraded):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            commands = {await ready() for _ in range(2)}
            store = SandboxWorkerStore(engines[0], timedelta(seconds=15))
            worker = await store.register()
            results = await asyncio.gather(*(store.claim(worker) for _ in range(6)))
            [winner] = [lease for lease in results if lease is not None]
            assert await store.claim(worker) is None
            separate_registration = await store.register()
            assert separate_registration != worker
            other = await store.claim(separate_registration)
            assert other
            assert {winner.owner.execution_id, other.owner.execution_id} == commands
            for ident in commands:
                state, attempts = await snapshot(db, ident)
                assert state["generation"] == len(attempts) == 1
                assert attempts[0]["send_marked_at"] is None

    asyncio.run(check())


# In the test child, complete the real PostgreSQL transaction before injecting a
# lost acknowledgment. Other operations use the original transaction methods.
_LOST_ACK_PROGRAM = """
import sys
from sqlalchemy.ext.asyncio import AsyncTransaction
from gateway.execution_worker import main
from gateway.sandbox_worker_store import SandboxWorkerStore

mode = sys.argv[1]
operating = None
original_operate = SandboxWorkerStore._operate
original_exit = AsyncTransaction.__aexit__

async def operate(self, operation, *args, **kwargs):
    global operating
    operating = operation
    try:
        return await original_operate(self, operation, *args, **kwargs)
    finally:
        operating = None

async def lose_ack(self, *failure):
    await original_exit(self, *failure)
    if operating == mode and failure[0] is None:
        print('postgres_commit_completed_before_fault', flush=True)
        raise OSError('injected acknowledgment loss')

SandboxWorkerStore._operate = operate
AsyncTransaction.__aexit__ = lose_ack
raise SystemExit(main())
"""


@pytest.mark.parametrize("operation", ["claim", "renew"])
def test_process_reports_committed_ownership_ack_loss(upgraded, operation):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            ident, untouched = await ready(), await ready()
            before = await snapshot(db, untouched)
            process = await start_worker(
                engines[0],
                program=(_LOST_ACK_PROGRAM, operation),
                lease_seconds=10,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), 10)
                assert (
                    process.returncode == 3
                    and output.count(b"postgres_commit_completed_before_fault") == 1
                )
                assert (
                    f"worker_ownership_unconfirmed operation={operation}".encode()
                    in output
                )
                assert b"ownership_release" not in output
                state, [attempt] = await snapshot(db, ident)
                assert state["state"] == "CLAIMED" and state["generation"] == 1
                assert state["receipts"] == 0 and not attempt["send_marked_at"]
                assert (
                    state["lease_expires_at"]
                    > attempt["claimed_at"] + timedelta(seconds=10)
                    if operation == "renew"
                    else state["lease_expires_at"]
                    == attempt["claimed_at"] + timedelta(seconds=10)
                )
                assert await snapshot(db, untouched) == before
            finally:
                await stop_worker(process, signal.SIGKILL)

    asyncio.run(check())


def test_process_shutdown_is_bounded_while_database_call_is_blocked(upgraded):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            ident = await ready()
            before = await snapshot(db, ident)
            async with db.begin() as blocker:
                [pid] = await rows(blocker, "SELECT pg_backend_pid() AS pid")
                await rows(
                    blocker, "SELECT singleton FROM execution_dispatch_gate FOR UPDATE"
                )
                process = await start_worker(engines[0], drain_seconds=0.2)
                try:
                    await event_from(process, b"worker_ready")
                    await wait_for_database_block(db, pid["pid"])
                    process.send_signal(signal.SIGTERM)
                    output, _ = await asyncio.wait_for(
                        process.communicate(), timeout=1.5
                    )
                    assert (
                        process.returncode == 0 and b"worker_drain_deadline" in output
                    )
                    assert b"ownership_claimed" not in output
                finally:
                    await stop_worker(process, signal.SIGKILL)
            assert await snapshot(db, ident) == before

    asyncio.run(check())


@pytest.mark.parametrize("operation", ["claim", "renew"])
def test_sigterm_after_commit_with_unconfirmed_ownership_exits_three(
    upgraded, operation
):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            ident, untouched = await ready(), await ready()
            other_before = await snapshot(db, untouched)
            # Pause the child after the real commit, before the store returns its result.
            program = _LOST_ACK_PROGRAM.replace(
                "raise OSError('injected acknowledgment loss')",
                "await __import__('asyncio').Event().wait()",
            )
            process = await start_worker(
                engines[0],
                program=(program, operation),
                lease_seconds=10,
                drain_seconds=0.2,
            )
            try:
                await event_from(process, b"postgres_commit_completed_before_fault")
                before = await snapshot(db, ident)
                state, [attempt] = before
                assert state["state"] == "CLAIMED"
                assert state["generation"] == attempt["generation"] == 1
                assert state["lease_owner"] == attempt["worker_id"]
                assert state["receipts"] == 0 and attempt["send_marked_at"] is None
                assert attempt["outcome"] is None
                initial_expiry = attempt["claimed_at"] + timedelta(seconds=10)
                if operation == "claim":
                    assert state["lease_expires_at"] == initial_expiry
                else:
                    assert state["lease_expires_at"] > initial_expiry
                process.send_signal(signal.SIGTERM)
                output, _ = await asyncio.wait_for(process.communicate(), timeout=1.5)
                assert process.returncode == 3, output
                assert (
                    f"worker_ownership_unconfirmed operation={operation}".encode()
                    in output
                )
                assert b"ownership_release" not in output
                assert await snapshot(db, ident) == before
                assert await snapshot(db, untouched) == other_before
            finally:
                await stop_worker(process, signal.SIGKILL)

    asyncio.run(check())


def test_sigterm_during_blocked_renewal_exits_three(upgraded):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            ident = await ready()
            process = await start_worker(
                engines[0], lease_seconds=10, renew_seconds=1, drain_seconds=0.2
            )
            try:
                await event_from(process, b"ownership_claimed")
                before = await snapshot(db, ident)
                state, [attempt] = before
                assert state["lease_owner"] == attempt["worker_id"]
                assert state["generation"] == attempt["generation"] == 1
                assert state["lease_expires_at"] == attempt["claimed_at"] + timedelta(
                    seconds=10
                )
                async with db.begin() as blocker:
                    [pid] = await rows(blocker, "SELECT pg_backend_pid() AS pid")
                    await rows(
                        blocker,
                        "SELECT id FROM executions WHERE id=:id FOR UPDATE",
                        id=ident,
                    )
                    await wait_for_database_block(db, pid["pid"])
                    process.send_signal(signal.SIGTERM)
                    # Keep holding the row lock until the child actually exits.
                    output, _ = await asyncio.wait_for(
                        process.communicate(), timeout=1.5
                    )
                    assert process.returncode == 3, output
                    assert b"worker_ownership_unconfirmed operation=renew" in output
                    assert b"ownership_release" not in output
                assert await snapshot(db, ident) == before
                assert state["receipts"] == 0 and attempt["send_marked_at"] is None
            finally:
                await stop_worker(process, signal.SIGKILL)

    asyncio.run(check())


_CLAIM_BARRIER = """
import asyncio
import sys
from gateway.execution_worker import main
from gateway.sandbox_worker_store import SandboxWorkerStore

original_claim = SandboxWorkerStore.claim

async def claim(self, *args):
    print('claim_ready_for_lock', flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    return await original_claim(self, *args)

SandboxWorkerStore.claim = claim
raise SystemExit(main())
"""


def disposable_container(engine):
    names = docker(
        "ps", "--filter", "name=toolmesh-m911-schema-", "--format", "{{.Names}}"
    ).stdout.splitlines()
    containers = json.loads(docker("inspect", *names).stdout)
    [name] = [
        item["Name"].lstrip("/")
        for item in containers
        if any(
            binding["HostPort"] == str(engine.url.port)
            for binding in item["NetworkSettings"]["Ports"].get("5432/tcp") or []
        )
    ]
    return name


@pytest.mark.parametrize("operation", ["claim", "renew"])
def test_sigterm_hard_deadline_with_postgres_paused(upgraded, operation):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            ident, untouched = await ready(), await ready()
            untouched_before = await snapshot(db, untouched)
            container = disposable_container(db)
            process = await start_worker(
                engines[0],
                program=(_CLAIM_BARRIER,) if operation == "claim" else None,
                lease_seconds=10,
                renew_seconds=1,
                drain_seconds=0.2,
            )
            try:
                await event_from(
                    process,
                    b"claim_ready_for_lock"
                    if operation == "claim"
                    else b"ownership_claimed",
                )
                before = await snapshot(db, ident)
                state, attempts = before
                assert (state["state"], state["generation"], len(attempts)) == (
                    ("READY", 0, 0) if operation == "claim" else ("CLAIMED", 1, 1)
                )
                if attempts:
                    assert attempts[0]["worker_id"] == state["lease_owner"]
                    assert attempts[0]["send_marked_at"] is None
                async with db.begin() as blocker:
                    [pid] = await rows(blocker, "SELECT pg_backend_pid() AS pid")
                    if operation == "claim":
                        await rows(
                            blocker,
                            "SELECT singleton FROM execution_dispatch_gate FOR UPDATE",
                        )
                        process.stdin.write(b"start\n")
                        await process.stdin.drain()
                    else:
                        await rows(
                            blocker,
                            "SELECT id FROM executions WHERE id=:id FOR UPDATE",
                            id=ident,
                        )
                    await wait_for_database_block(db, pid["pid"])
                    docker("pause", container)
                    try:
                        clock = asyncio.get_running_loop()
                        start = clock.time()
                        process.send_signal(signal.SIGTERM)
                        # A repeated signal during driver cancellation must not
                        # extend the hard deadline or use the OS default status.
                        await asyncio.sleep(0.23)
                        if process.returncode is None:
                            process.send_signal(signal.SIGTERM)
                        output, _ = await asyncio.wait_for(
                            process.communicate(), timeout=1.0
                        )
                        elapsed = clock.time() - start
                        assert process.returncode == 3, output
                        assert b"worker_drain_deadline" in output
                        assert b"ownership_release" not in output
                        # Allow 350 ms of process scheduling and reaping overhead
                        # beyond the configured 200 ms + 250 ms hard deadline.
                        assert elapsed < 0.8
                        assert (
                            docker(
                                "inspect", "-f", "{{.State.Paused}}", container
                            ).stdout.strip()
                            == "true"
                        )
                        print(
                            f"paused PostgreSQL {operation}: exit=3 elapsed={elapsed:.3f}s",
                            flush=True,
                        )
                    finally:
                        docker("unpause", container)
                assert await snapshot(db, ident) == before
                assert await snapshot(db, untouched) == untouched_before
                assert state["receipts"] == 0
            finally:
                await stop_worker(process, signal.SIGKILL)

    asyncio.run(check())


@pytest.mark.parametrize("disposal_fault", ["none", "raise", "cancel"])
def test_confirmed_renewal_during_sigterm_drain_releases(upgraded, disposal_fault):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            ident = await ready()
            program = _LOST_ACK_PROGRAM.replace(
                "import sys\n",
                """import asyncio
import sys
from gateway.execution_worker import ShutdownDeadline
original_arm = ShutdownDeadline.arm
def announce_signal(self, drain_seconds):
    original_arm(self, drain_seconds)
    print('shutdown_armed', flush=True)
ShutdownDeadline.arm = announce_signal
""",
                1,
            ).replace(
                "raise OSError('injected acknowledgment loss')",
                "await asyncio.to_thread(sys.stdin.readline)",
            )
            if disposal_fault != "none":
                program = program.replace(
                    "import sys\n", "import sys\n" + _POOL_DISPOSAL_FAULT, 1
                )
            process = await start_worker(
                engines[0],
                program=(program, "renew", disposal_fault),
                lease_seconds=10,
                drain_seconds=1,
            )
            try:
                await event_from(process, b"postgres_commit_completed_before_fault")
                before, [attempt] = await snapshot(db, ident)
                assert before["state"] == "CLAIMED" and attempt["outcome"] is None
                assert before["lease_expires_at"] > attempt["claimed_at"] + timedelta(
                    seconds=10
                )
                process.send_signal(signal.SIGTERM)
                await event_from(process, b"shutdown_armed")
                output, _ = await asyncio.wait_for(
                    process.communicate(b"ack\n"), timeout=1.5
                )
                assert process.returncode == (0 if disposal_fault == "none" else 1), (
                    output
                )
                assert b"ownership_release result=released" in output
                if disposal_fault == "raise":
                    assert b"worker_pool_cleanup_failed error_type=OSError" in output
                elif disposal_fault == "cancel":
                    assert b"asyncio.exceptions.CancelledError" in output
                    assert b"worker_ownership_unconfirmed" not in output
                state, [closed] = await snapshot(db, ident)
                assert state["state"] == "READY" and state["lease_owner"] is None
                assert (
                    state["generation"]
                    == closed["generation"]
                    == attempt["generation"]
                    == 1
                )
                assert (
                    closed["outcome"] == "NOT_SENT" and closed["send_marked_at"] is None
                )
                assert state["receipts"] == 0
            finally:
                await stop_worker(process, signal.SIGKILL)

    asyncio.run(check())


_POOL_DISPOSAL_FAULT = """
import asyncio
import os
import signal
from asyncio.unix_events import _UnixSelectorEventLoop
from sqlalchemy.ext.asyncio import AsyncEngine
from gateway import execution_worker

async def fault_dispose(self, *args, **kwargs):
    print("pool_dispose_entered", flush=True)
    if sys.argv[2] in {"raise", "second-signal"}:
        raise OSError("injected pool disposal failure")
    if sys.argv[2] == "cancel":
        raise asyncio.CancelledError()
    await asyncio.Event().wait()

AsyncEngine.dispose = fault_dispose

if sys.argv[2] == "second-signal":
    original_remove = _UnixSelectorEventLoop.remove_signal_handler
    original_run = execution_worker.run
    signal_sent = False

    def repeat_sigterm():
        global signal_sent
        if not signal_sent:
            signal_sent = True
            print("second_sigterm_at_signal_teardown", flush=True)
            os.kill(os.getpid(), signal.SIGTERM)

    def remove(self, sig):
        removed = original_remove(self, sig)
        if sig == signal.SIGTERM and removed:
            repeat_sigterm()
        return removed

    async def run(*args, **kwargs):
        try:
            await original_run(*args, **kwargs)
        finally:
            # The corrected worker retains its process-level handler here.
            repeat_sigterm()

    _UnixSelectorEventLoop.remove_signal_handler = remove
    execution_worker.run = run
"""


@pytest.mark.parametrize(
    "operation,fault",
    [
        ("claim", "raise"),
        ("renew", "raise"),
        ("claim", "cancel"),
        ("renew", "cancel"),
        ("claim", "second-signal"),
        ("renew", "second-signal"),
        ("renew", "hang"),
    ],
)
def test_unconfirmed_ownership_survives_pool_disposal_fault(upgraded, operation, fault):
    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            ident, untouched = await ready(), await ready()
            untouched_before = await snapshot(db, untouched)
            program = _LOST_ACK_PROGRAM.replace(
                "import sys\n", "import sys\n" + _POOL_DISPOSAL_FAULT, 1
            ).replace(
                "raise OSError('injected acknowledgment loss')",
                "await asyncio.Event().wait()",
            )
            process = await start_worker(
                engines[0],
                program=(program, operation, fault),
                lease_seconds=10,
                drain_seconds=0.2,
            )
            try:
                await event_from(process, b"postgres_commit_completed_before_fault")
                before = await snapshot(db, ident)
                state, [attempt] = before
                assert state["state"] == "CLAIMED"
                assert state["lease_owner"] == attempt["worker_id"]
                assert state["generation"] == attempt["generation"] == 1
                assert state["receipts"] == 0 and attempt["send_marked_at"] is None
                assert attempt["outcome"] is None
                loop = asyncio.get_running_loop()
                start = loop.time()
                process.send_signal(signal.SIGTERM)
                output, _ = await asyncio.wait_for(process.communicate(), timeout=1.5)
                elapsed = loop.time() - start
                assert process.returncode == 3, output
                assert output.count(b"pool_dispose_entered") == 1
                assert b"ownership_release" not in output
                if fault != "hang":
                    error = "CancelledError" if fault == "cancel" else "OSError"
                    assert (
                        f"worker_pool_cleanup_failed error_type={error}".encode()
                        in output
                    )
                    if fault == "second-signal":
                        assert output.count(b"second_sigterm_at_signal_teardown") == 1
                    assert (
                        f"worker_ownership_unconfirmed operation={operation}".encode()
                        in output
                    )
                else:
                    assert b"worker_drain_deadline" in output
                    assert 0.2 <= elapsed < 0.8
                assert await snapshot(db, ident) == before
                assert await snapshot(db, untouched) == untouched_before
                print(
                    f"pool disposal {fault} after committed {operation}: exit=3 elapsed={elapsed:.3f}s",
                    flush=True,
                )
            finally:
                await stop_worker(process, signal.SIGKILL)

    asyncio.run(check())


_NEAR_DEADLINE_PROGRAM = r"""
import asyncio
import os
import sys
from time import monotonic_ns
from sqlalchemy.ext.asyncio import AsyncEngine
from gateway.execution_worker import ShutdownDeadline, main

original_arm = ShutdownDeadline.arm
original_exit = ShutdownDeadline._exit

def arm(self, drain_seconds):
    print(f'signal_received_ns={monotonic_ns()}', flush=True)
    original_arm(self, drain_seconds)

def expire(self):
    os.write(1, f'watchdog_fired_ns={monotonic_ns()}\n'.encode())
    original_exit(self)

async def wait_in_dispose(self, *args, **kwargs):
    await asyncio.Event().wait()

ShutdownDeadline.arm = arm
ShutdownDeadline._exit = expire
AsyncEngine.dispose = wait_in_dispose
raise SystemExit(main())
"""


def test_postgres_recovery_near_watchdog_deadline_preserves_status(upgraded):
    from time import monotonic_ns

    async def check():
        async with restricted(upgraded) as (db, engines, ready, _):
            ident = await ready()
            container = disposable_container(db)
            process = await start_worker(
                engines[0],
                program=(_NEAR_DEADLINE_PROGRAM,),
                lease_seconds=10,
                renew_seconds=1,
                drain_seconds=0.2,
            )
            try:
                await event_from(process, b"ownership_claimed")
                before = await snapshot(db, ident)
                async with db.begin() as blocker:
                    [pid] = await rows(blocker, "SELECT pg_backend_pid() AS pid")
                    await rows(
                        blocker,
                        "SELECT id FROM executions WHERE id=:id FOR UPDATE",
                        id=ident,
                    )
                    blocked_pid = await wait_for_database_block(db, pid["pid"])
                    # Keep the control process running before starting the
                    # clock, so Docker CLI startup is outside the race.
                    control = await asyncio.create_subprocess_exec(
                        "docker",
                        "exec",
                        "-i",
                        "-u",
                        "postgres",
                        container,
                        "sh",
                        "-ec",
                        "trap 'kill -CONT \"$1\" 2>/dev/null || true' EXIT; "
                        'kill -STOP "$1"; echo backend_paused; read -r resume; '
                        'kill -CONT "$1"; echo backend_resumed',
                        "sh",
                        str(blocked_pid),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    try:
                        await event_from(control, b"backend_paused")
                        process.send_signal(signal.SIGTERM)
                        received = await event_from(process, b"signal_received_ns=")
                        signal_ns = int(received.split(b"=")[1])
                        # Resume after cooperative cancellation but before the
                        # nominal 450 ms hard deadline. Keep the row lock held.
                        await asyncio.sleep(
                            max(0, (signal_ns + 370_000_000 - monotonic_ns()) / 1e9)
                        )
                        control.stdin.write(b"resume\n")
                        await control.stdin.drain()
                        await event_from(control, b"backend_resumed")
                        resumed_ns = monotonic_ns()
                    finally:
                        if control.stdin is not None:
                            control.stdin.close()
                        assert await asyncio.wait_for(control.wait(), timeout=2) == 0
                    output, _ = await asyncio.wait_for(
                        process.communicate(), timeout=1.5
                    )
                    observed_ns = monotonic_ns()
                    assert process.returncode == 3, output
                    [watchdog] = [
                        line
                        for line in output.splitlines()
                        if line.startswith(b"watchdog_fired_ns=")
                    ]
                    watchdog_ns = int(watchdog.split(b"=")[1])
                    assert signal_ns < resumed_ns < watchdog_ns <= observed_ns
                    assert 0.35 <= (resumed_ns - signal_ns) / 1e9
                    assert 0.40 <= (watchdog_ns - signal_ns) / 1e9 < 0.8
                    assert b"ownership_release" not in output
                    print(
                        f"signal_received_ns={signal_ns} postgres_resumed_ns={resumed_ns} "
                        f"watchdog_fired_ns={watchdog_ns} parent_observed_exit_ns={observed_ns} "
                        f"exit=3 elapsed={(observed_ns - signal_ns) / 1e9:.3f}s",
                        flush=True,
                    )
                assert await snapshot(db, ident) == before
                state, [attempt] = before
                assert state["generation"] == attempt["generation"] == 1
                assert state["lease_owner"] == attempt["worker_id"]
                assert state["receipts"] == 0 and attempt["send_marked_at"] is None
            finally:
                await stop_worker(process, signal.SIGKILL)

    asyncio.run(check())
