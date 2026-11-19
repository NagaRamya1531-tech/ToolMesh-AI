"""Migration checks against a PostgreSQL container owned by this test session."""

import asyncio
import os
import subprocess
import sys
import time
from uuid import uuid4

import asyncpg
import pytest

IMAGE = os.getenv("TOOLMESH_TEST_SCHEMA_IMAGE")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires disposable PostgreSQL image"
)

EXECUTION = "37e17e14-bfe0-47e8-9877-b9b838179a58"
SECOND = "450c2cfd-87f9-4198-9f10-c726534763b6"
WORKER = "6ea448c6-4cca-4956-a5f0-9ceef770486a"
SCOPE = "'default','finance-agent','C-102','payments.issue_refund',1"
DIGEST = "decode(repeat('ab', 32), 'hex')"
LEDGER_TABLES = (
    "execution_commands",
    "execution_request_keys",
    "execution_legacy_key_aliases",
    "execution_attempts",
    "execution_receipts",
    "execution_approval_entitlements",
    "execution_authorization_guard",
    "execution_dispatch_gate",
)
COMMAND = f"""
    INSERT INTO execution_commands (
        execution_id, workspace_id, requester, customer_id, tool_id, tool_version,
        contract_digest, state
    ) VALUES ('{EXECUTION}', {SCOPE}, {DIGEST}, 'UNKNOWN')
"""
KEY = f"""
    INSERT INTO execution_request_keys (
        execution_id, workspace_id, requester, customer_id, tool_id,
        tool_version, key_digest, intent_digest, digest_key_version, canonicalizer_version
    ) VALUES ('{EXECUTION}', {SCOPE}, {DIGEST}, {DIGEST}, 1, 1)
"""
ATTEMPT = f"""
    INSERT INTO execution_attempts (execution_id, generation, worker_id, kind, claimed_at)
    VALUES ('{EXECUTION}', 1, '{WORKER}', 'VERIFY', now())
"""
CONFIRM_ATTEMPT = f"""
    UPDATE execution_attempts SET finished_at=now(), outcome='SUCCEEDED'
    WHERE execution_id='{EXECUTION}' AND generation=1
"""
RECEIPT = f"""
    INSERT INTO execution_receipts (
        execution_id, generation, outcome, evidence_source, external_reference, evidence_digest
    ) VALUES ('{EXECUTION}', 1, 'SUCCEEDED', 'LOOKUP', 'synthetic-refund', {DIGEST})
"""


def docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True, timeout=120
    )


def migrate(env, action, revision, *, check=True):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", action, revision],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


@pytest.fixture(scope="module", params=["fresh", "upgrade"])
def sandbox(request):
    name = f"toolmesh-m911-schema-{uuid4().hex[:12]}"
    passwords = {
        f"TOOLMESH_{domain}_DB_PASSWORD": "synthetic-ledger-test"
        for domain in (
            "PAYMENTS",
            "SUPPORT",
            "COMMUNICATION",
            "INVENTORY",
            "IT",
            "HR",
            "ANALYTICS",
        )
    }
    docker(
        "run",
        "--detach",
        "--name",
        name,
        "--publish",
        "127.0.0.1::5432",
        "-e",
        "POSTGRES_DB=toolmesh",
        "-e",
        "POSTGRES_USER=toolmesh_migrator",
        "-e",
        "POSTGRES_PASSWORD=synthetic-ledger-test",
        "-e",
        "TOOLMESH_APP_PASSWORD=synthetic-app-test",
        str(IMAGE),
    )
    try:
        for _ in range(60):
            if (
                docker(
                    "exec",
                    name,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "toolmesh_migrator",
                    "-d",
                    "toolmesh",
                    check=False,
                ).returncode
                == 0
            ):
                break
            time.sleep(0.5)
        else:
            pytest.fail("disposable PostgreSQL did not become ready")
        port = docker("port", name, "5432/tcp").stdout.strip().rsplit(":", 1)[1]
        url = f"postgresql://toolmesh_migrator:synthetic-ledger-test@127.0.0.1:{port}/toolmesh"
        env = {
            **os.environ,
            **passwords,
            "TOOLMESH_MIGRATION_URL": url.replace("postgresql:", "postgresql+asyncpg:"),
        }
        if request.param == "upgrade":
            migrate(env, "upgrade", "0009")
            asyncio.run(seed_history(url))
            before = asyncio.run(snapshot(url))
            assert [len(rows) for rows in before] == [2, 2, 4, 2]
        else:
            before = None
        migrate(env, "upgrade", "0010")
        if before is None:
            asyncio.run(seed_history(url))
        yield url, env, before
    finally:
        docker("rm", "-f", "-v", name)


async def seed_history(url):
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(
            f"""
            INSERT INTO executions (id, principal, tool_id, version, status)
            VALUES ('{EXECUTION}', 'finance-agent', 'payments.issue_refund', 1, 'RECEIVED'),
                   ('{SECOND}', 'finance-agent', 'payments.issue_refund', 1, 'RECEIVED');
            INSERT INTO approvals (
                id, requester, tool_id, version, customer_id, arguments, intent_hash,
                request_key, status, decided_by, decision_reason, decided_at
            ) VALUES
                ('{EXECUTION}', 'finance-agent', 'payments.issue_refund', 1, 'C-102',
                    '{{"customer_id":"C-102"}}', 'synthetic', 'legacy-one',
                    'APPROVED', 'refund-approver', 'sandbox decision', now()),
                ('{SECOND}', 'finance-agent', 'payments.issue_refund', 1, 'C-102',
                    '{{"customer_id":"C-102"}}', 'synthetic', 'legacy-two',
                    'PENDING', NULL, NULL, NULL);
            UPDATE executions SET status='RUNNING' WHERE id='{EXECUTION}';
            UPDATE executions SET status='PENDING_APPROVAL' WHERE id='{SECOND}';
            INSERT INTO audit_events(actor,action,resource_id)
            VALUES ('refund-approver','REFUND_APPROVED','{EXECUTION}'),
                   ('finance-agent','REFUND_APPROVAL_REQUESTED','{SECOND}');
            """
        )
    finally:
        await conn.close()


async def snapshot(url):
    conn = await asyncpg.connect(url)
    try:
        return [
            await conn.fetch(f"SELECT * FROM {table} ORDER BY id")
            for table in ("executions", "approvals", "execution_events", "audit_events")
        ]
    finally:
        await conn.close()


def test_install_preserves_history_and_leaves_dispatch_legacy(sandbox):
    url, _, before = sandbox

    async def check():
        if before is not None:
            assert await snapshot(url) == before
        conn = await asyncpg.connect(url)
        try:
            assert (
                await conn.fetchval("SELECT version_num FROM alembic_version") == "0010"
            )
            assert (
                await conn.fetchval("SELECT mode FROM execution_dispatch_gate")
                == "LEGACY"
            )
            assert (
                await conn.fetchval(
                    "SELECT revision FROM execution_authorization_guard"
                )
                == 1
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM execution_approval_entitlements"
                )
                == 2
            )
            for table in (
                "execution_commands",
                "execution_attempts",
                "execution_receipts",
            ):
                assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 0
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM pg_indexes WHERE indexname IN "
                    "('execution_commands_ready','execution_commands_expired_lease','execution_commands_verify')"
                )
                == 3
            )
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute(
                    "UPDATE executions SET status='UNKNOWN' WHERE id=$1", EXECUTION
                )
                assert (
                    await conn.fetchval(
                        "SELECT status FROM execution_events WHERE execution_id=$1 ORDER BY id DESC LIMIT 1",
                        EXECUTION,
                    )
                    == "UNKNOWN"
                )
            finally:
                await tx.rollback()
        finally:
            await conn.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    ("setup", "statement", "error"),
    [
        ([COMMAND], COMMAND, asyncpg.UniqueViolationError),
        ([], COMMAND.replace(EXECUTION, WORKER), asyncpg.ForeignKeyViolationError),
        ([], COMMAND.replace("C-102", "C-103"), asyncpg.ForeignKeyViolationError),
        ([], COMMAND.replace("'UNKNOWN'", "'RUNNING'"), asyncpg.CheckViolationError),
        ([], COMMAND.replace("'UNKNOWN'", "'READY'"), asyncpg.CheckViolationError),
        ([], COMMAND.replace("'UNKNOWN'", "'CLAIMED'"), asyncpg.CheckViolationError),
        ([KEY], KEY.replace(EXECUTION, SECOND), asyncpg.UniqueViolationError),
        (
            [KEY],
            KEY.replace(EXECUTION, SECOND).replace(", 1, 1)", ", 2, 1)"),
            asyncpg.UniqueViolationError,
        ),
        ([COMMAND, ATTEMPT], ATTEMPT, asyncpg.UniqueViolationError),
        ([], ATTEMPT, asyncpg.ForeignKeyViolationError),
        (
            [COMMAND, ATTEMPT],
            f"UPDATE execution_attempts SET send_marked_at=now(), admission_revision=1 WHERE execution_id='{EXECUTION}'",
            asyncpg.CheckViolationError,
        ),
        (
            [COMMAND, ATTEMPT, CONFIRM_ATTEMPT, RECEIPT],
            RECEIPT,
            asyncpg.UniqueViolationError,
        ),
        ([COMMAND, ATTEMPT], RECEIPT, asyncpg.ForeignKeyViolationError),
        (
            [COMMAND, ATTEMPT, CONFIRM_ATTEMPT],
            RECEIPT.replace("'SUCCEEDED'", "'FAILED'"),
            asyncpg.ForeignKeyViolationError,
        ),
        (
            [COMMAND, ATTEMPT],
            RECEIPT.replace(", 1, 'SUCCEEDED'", ", 2, 'SUCCEEDED'"),
            asyncpg.ForeignKeyViolationError,
        ),
        (
            [COMMAND, ATTEMPT],
            RECEIPT.replace("'SUCCEEDED'", "'UNKNOWN'"),
            asyncpg.CheckViolationError,
        ),
        (
            [
                KEY,
                f"INSERT INTO execution_legacy_key_aliases VALUES ('finance-agent','payments.issue_refund',{DIGEST},'{EXECUTION}')",
            ],
            f"INSERT INTO execution_legacy_key_aliases VALUES ('finance-agent','payments.issue_refund',{DIGEST},'{EXECUTION}')",
            asyncpg.UniqueViolationError,
        ),
        (
            [KEY],
            f"INSERT INTO execution_legacy_key_aliases VALUES ('other-agent','payments.issue_refund',{DIGEST},'{EXECUTION}')",
            asyncpg.ForeignKeyViolationError,
        ),
        (
            [
                COMMAND,
                ATTEMPT.replace("'VERIFY', now()", "'SEND', now()"),
                f"UPDATE execution_attempts SET send_marked_at=now(), admission_revision=1 WHERE execution_id='{EXECUTION}'",
            ],
            ATTEMPT.replace(", 1,", ", 2,")
            .replace(
                "kind, claimed_at",
                "kind, claimed_at, send_marked_at, admission_revision",
            )
            .replace("'VERIFY', now()", "'SEND', now(), now(), 1"),
            asyncpg.UniqueViolationError,
        ),
    ],
)
def test_schema_constraints(sandbox, setup, statement, error):
    url, _, _ = sandbox

    async def check():
        conn = await asyncpg.connect(url)
        tx = conn.transaction()
        await tx.start()
        try:
            for sql in setup:
                await conn.execute(sql)
            with pytest.raises(error):
                await conn.execute(statement)
        finally:
            await tx.rollback()
            await conn.close()

    asyncio.run(check())


async def execute_in_sandbox(url, sql, *args):
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


async def assert_schema(url, revision, *, gate=None, commands=0):
    conn = await asyncpg.connect(url)
    try:
        assert (
            await conn.fetchval("SELECT version_num FROM alembic_version") == revision
        )
        for table in LEDGER_TABLES:
            exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
            assert exists == (revision == "0010"), table
        if revision == "0010":
            assert (
                await conn.fetchval("SELECT mode FROM execution_dispatch_gate") == gate
            )
            assert (
                await conn.fetchval(
                    "SELECT revision FROM execution_authorization_guard"
                )
                == 1
            )
            assert (
                await conn.fetchval("SELECT count(*) FROM execution_commands")
                == commands
            )
            assert (
                await conn.fetchval("SELECT count(*) FROM execution_request_keys") == 0
            )
    finally:
        await conn.close()


def test_downgrade_preserves_baseline_and_refuses_populated_ledger(sandbox):
    url, env, _ = sandbox
    before = asyncio.run(snapshot(url))
    migrate(env, "downgrade", "0009")
    asyncio.run(assert_schema(url, "0009"))
    assert asyncio.run(snapshot(url)) == before
    migrate(env, "upgrade", "0010")

    asyncio.run(execute_in_sandbox(url, COMMAND))
    try:
        result = migrate(env, "downgrade", "0009", check=False)
        assert result.returncode != 0
        assert (
            "Cannot downgrade a transferred or populated durable execution ledger"
            in result.stderr
        )
        asyncio.run(assert_schema(url, "0010", gate="LEGACY", commands=1))
        assert asyncio.run(snapshot(url)) == before
    finally:
        asyncio.run(
            execute_in_sandbox(
                url, f"DELETE FROM execution_commands WHERE execution_id='{EXECUTION}'"
            )
        )


def test_gateway_has_no_effective_ledger_privileges(sandbox):
    url, _, _ = sandbox

    async def check():
        conn = await asyncpg.connect(url)
        try:
            await conn.execute("SET ROLE toolmesh_app")
            assert await conn.fetchval("SELECT current_user") == "toolmesh_app"
            for table in LEDGER_TABLES:
                qualified = f"public.{table}"
                # These predicates include PUBLIC and inherited grants.
                for privilege in (
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "TRUNCATE",
                    "REFERENCES",
                    "TRIGGER",
                ):
                    assert not await conn.fetchval(
                        "SELECT has_table_privilege(current_user, $1, $2)",
                        qualified,
                        privilege,
                    ), (table, privilege)
                for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    assert not await conn.fetchval(
                        "SELECT has_any_column_privilege(current_user, $1, $2)",
                        qualified,
                        privilege,
                    ), (table, privilege, "column grant")
            assert await conn.fetchval(
                "SELECT has_table_privilege(current_user, 'public.execution_events', 'SELECT')"
            )
        finally:
            await conn.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "tool,requester,other",
    [
        ("payments.issue_refund", "finance-agent", "it.restart_service"),
        ("it.restart_service", "it-agent", "payments.issue_refund"),
    ],
)
def test_protected_tool_binding(sandbox, tool, requester, other):
    url, _, _ = sandbox

    async def check():
        conn = await asyncpg.connect(url)
        execution = uuid4()
        try:
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute(
                    "INSERT INTO executions (id, principal, tool_id, version, status) "
                    "VALUES ($1,$2,$3,1,'PENDING_APPROVAL')",
                    execution,
                    requester,
                    tool,
                )
                await conn.execute(
                    """
                    INSERT INTO approvals (
                        id, requester, customer_id, tool_id, version, arguments,
                        intent_hash, request_key, status
                    ) VALUES ($1,$2,'C-102',$3,1,'{}','synthetic','tool-binding','PENDING')
                    """,
                    execution,
                    requester,
                    tool,
                )
                command = """
                    INSERT INTO execution_commands (
                        execution_id, workspace_id, requester, customer_id, tool_id,
                        tool_version, contract_digest, state
                    ) VALUES ($1,'default',$2,'C-102',$3,$4,decode(repeat('ab',32),'hex'),'UNKNOWN')
                    RETURNING tool_id, tool_version
                """
                with pytest.raises(asyncpg.CheckViolationError):
                    async with conn.transaction():
                        await conn.fetchrow(
                            command, execution, requester, "support.create_ticket", 1
                        )
                with pytest.raises(asyncpg.ForeignKeyViolationError) as mismatch:
                    async with conn.transaction():
                        await conn.fetchrow(command, execution, requester, other, 1)
                assert "requester_tool_id" in mismatch.value.constraint_name

                # Legacy approvals can store a nonexistent version. Match that row to
                # exercise the catalog FK separately from the approval-binding FK.
                with pytest.raises(asyncpg.ForeignKeyViolationError) as version:
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE approvals SET version=999 WHERE id=$1", execution
                        )
                        await conn.fetchrow(command, execution, requester, tool, 999)
                assert (
                    version.value.constraint_name
                    == "execution_commands_tool_id_tool_version_fkey"
                )

                accepted = await conn.fetchrow(command, execution, requester, tool, 1)
                assert tuple(accepted) == (tool, 1)
                await conn.execute(
                    """
                    INSERT INTO execution_request_keys (
                        execution_id, workspace_id, requester, customer_id, tool_id,
                        tool_version, key_digest, intent_digest, digest_key_version,
                        canonicalizer_version
                    ) VALUES ($1,'default',$2,'C-102',$3,1,decode(repeat('ab',32),'hex'),
                        decode(repeat('cd',32),'hex'),1,1)
                    """,
                    execution,
                    requester,
                    tool,
                )
            finally:
                await tx.rollback()
        finally:
            await conn.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "table", ["execution_authorization_guard", "execution_dispatch_gate"]
)
def test_singleton_seed_and_duplicate_constraints(sandbox, table):
    url, _, _ = sandbox

    async def check():
        conn = await asyncpg.connect(url)
        try:
            assert await conn.fetch(f"SELECT singleton FROM {table}") == [(True,)]
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(f"INSERT INTO {table} DEFAULT VALUES")
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(f"INSERT INTO {table} (singleton) VALUES (false)")
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute(f"DELETE FROM {table}")
                # A singleton key bounds cardinality above, not below. Only a
                # privileged test connection can remove the seeded record.
                assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 0
            finally:
                await tx.rollback()
            assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 1
        finally:
            await conn.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    ("table", "change"),
    [
        ("execution_commands", "ciphertext = decode('ab','hex')"),
        ("execution_commands", "nonce = decode('ab','hex')"),
        ("execution_commands", "lease_owner = NULL"),
        ("execution_commands", "lease_expires_at = NULL"),
        ("execution_commands", "generation = 0"),
        ("execution_commands", "state = 'SUCCEEDED'"),
        ("execution_commands", "verification_only = true"),
        ("execution_receipts", "wrapped_data_key = NULL"),
        ("execution_receipts", "nonce = decode('ab','hex')"),
        ("execution_receipts", "key_version = 0"),
    ],
)
def test_ledger_row_boundaries(sandbox, table, change):
    url, _, _ = sandbox

    async def check():
        conn = await asyncpg.connect(url)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(COMMAND)
            envelope = (
                "ciphertext=decode('ab','hex'), wrapped_data_key=decode('cd','hex'), "
                "nonce=decode(repeat('ef',12),'hex'), key_version=1"
            )
            if (
                table == "execution_commands"
                and change == "ciphertext = decode('ab','hex')"
            ):
                # Apply one field to an otherwise empty envelope.
                assert (
                    await conn.fetchval("SELECT ciphertext FROM execution_commands")
                    is None
                )
            elif table == "execution_commands":
                await conn.execute(
                    f"UPDATE execution_commands SET {envelope}, state='CLAIMED', "
                    "generation=1, lease_owner=$1, lease_expires_at=now()+interval '1 minute', "
                    "dispatch_not_after=now()+interval '2 minutes'",
                    WORKER,
                )
            else:
                for sql in (ATTEMPT, CONFIRM_ATTEMPT, RECEIPT):
                    await conn.execute(sql)
                await conn.execute(f"UPDATE execution_receipts SET {envelope}")
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(f"UPDATE {table} SET {change}")
        finally:
            await tx.rollback()
            await conn.close()

    asyncio.run(check())


@pytest.mark.parametrize("gate", ["PAUSED", None], ids=["paused", "missing"])
def test_gate_blocks_downgrade_of_empty_ledger(sandbox, gate):
    url, env, _ = sandbox
    history = asyncio.run(snapshot(url))

    asyncio.run(assert_schema(url, "0010", gate="LEGACY"))
    try:
        if gate is None:
            asyncio.run(execute_in_sandbox(url, "DELETE FROM execution_dispatch_gate"))
        else:
            asyncio.run(
                execute_in_sandbox(
                    url, "UPDATE execution_dispatch_gate SET mode=$1", gate
                )
            )
        result = migrate(env, "downgrade", "0009", check=False)
        assert result.returncode != 0
        assert (
            "Cannot downgrade a transferred or populated durable execution ledger"
            in result.stderr
        )
        asyncio.run(assert_schema(url, "0010", gate=gate))
        assert asyncio.run(snapshot(url)) == history
    finally:
        asyncio.run(
            execute_in_sandbox(
                url,
                "INSERT INTO execution_dispatch_gate VALUES (true,'LEGACY') "
                "ON CONFLICT (singleton) DO UPDATE SET mode='LEGACY'",
            )
        )
        asyncio.run(assert_schema(url, "0010", gate="LEGACY"))
