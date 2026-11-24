"""Normal REST and MCP protected admission on isolated PostgreSQL."""

import asyncio
import os
import socket
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from os import urandom
from time import monotonic
from uuid import UUID, uuid4

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_durable_execution_schema import IMAGE, migrate, snapshot
from test_durable_execution_schema import sandbox as sandbox
from test_execution_store import REQUEST

from gateway.execution_contracts import ProtectedIntentScope
from gateway.execution_crypto import (
    CommandDigests,
    PayloadContext,
    PayloadProtection,
    SandboxWrappingKeys,
    canonical_json,
)
from gateway.execution_store import CommandStore
from gateway.identity import digest
from shared.database import execute, rows

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="requires disposable PostgreSQL image"
)
PREFIX = "/api/v1"


@asynccontextmanager
async def api(env):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        program = """
import os
import uvicorn
from gateway.app import create_app
from gateway.runtime import Runtime

async def fixture_dispatch(self, tool, arguments, **kwargs):
    if tool == "payments.get_transaction":
        return {
            "id": arguments["transaction_id"], "order_id": "order-fixture",
            "customer_id": arguments["customer_id"], "amount_minor": 123,
            "currency": "USD", "status": "captured", "installment": None,
        }
    raise AssertionError("Package 1 must not make an inline domain call")

async def fixture_quota(self, principal, tool):
    # The focused test isolates the legacy precheck. Compose regressions use Redis.
    assert tool == "payments.get_transaction" or tool == "payments.issue_refund"

Runtime.downstream = fixture_dispatch
Runtime.quota = fixture_quota

if os.getenv("TOOLMESH_TEST_EXIT_AFTER_APPROVAL"):
    from gateway.execution_approval import ExecutionApprovals
    original = ExecutionApprovals.decide
    async def interrupt(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        os._exit(47)
    ExecutionApprovals.decide = interrupt

uvicorn.run(create_app(), fd=int(os.environ["TOOLMESH_TEST_FD"]), log_level="error")
"""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            env={**env, "TOOLMESH_TEST_FD": str(listener.fileno())},
            pass_fds=(listener.fileno(),),
        )
        try:
            async with httpx.AsyncClient(base_url=origin, timeout=12) as client:
                async with asyncio.timeout(15):
                    while True:
                        assert process.returncode is None
                        try:
                            if (await client.get("/healthz")).is_success:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.05)
                yield client, origin, process
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 8)
                except TimeoutError:
                    process.kill()
            await process.wait()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


async def set_mode(db, mode):
    async with db.begin() as conn:
        await execute(conn, "UPDATE execution_dispatch_gate SET mode=:mode", mode=mode)


async def assert_shared_row_boundary(url, durable, legacy):
    engine = create_async_engine(url, hide_parameters=True)
    try:
        for statement in (
            "UPDATE executions SET status='SUCCEEDED' WHERE id=:id",
            "UPDATE approvals SET status='APPROVED' WHERE id=:id",
            "DELETE FROM executions WHERE id=:id",
            "DELETE FROM approvals WHERE id=:id",
            "UPDATE execution_commands SET requester='support-agent' WHERE execution_id=:id",
            "DELETE FROM execution_commands WHERE execution_id=:id",
            """
            INSERT INTO executions(id,principal,status) VALUES (:id,'finance-agent','SUCCEEDED')
            ON CONFLICT (id) DO UPDATE SET status=excluded.status
            """,
            """
            INSERT INTO approvals(id,requester,tool_id,version,customer_id,arguments,intent_hash,request_key,status)
            VALUES (:id,'finance-agent','payments.issue_refund',1,'C-102','{}','fixture','replaced','APPROVED')
            ON CONFLICT (id) DO UPDATE SET status=excluded.status
            """,
        ):
            with pytest.raises(DBAPIError) as denied:
                async with engine.begin() as conn:
                    await execute(conn, statement, id=durable)
            assert denied.value.orig.sqlstate == "42501"
        async with engine.connect() as conn:
            async with conn.begin() as transaction:
                [updated] = await rows(
                    conn,
                    """
                    UPDATE executions SET status='REJECTED' WHERE id=:id RETURNING status
                """,
                    id=legacy,
                )
                assert updated["status"] == "REJECTED"
                [approved] = await rows(
                    conn,
                    """
                    UPDATE approvals SET status='REJECTED' WHERE id=:id RETURNING status
                """,
                    id=legacy,
                )
                assert approved["status"] == "REJECTED"
                await transaction.rollback()
    finally:
        await engine.dispose()


async def assert_failed_approval_retries(client, db, body, requester, approver):
    reason = {"reason": "fixture terminal retry"}
    for code in ("SYNTHETIC_DEFINITIVE_NO_EFFECT", "DISPATCH_DEADLINE_EXCEEDED"):
        submitted = await client.post(
            f"{PREFIX}/executions",
            json={
                **body,
                "idempotency_key": uuid4().hex,
            },
            headers=requester,
        )
        assert submitted.status_code == 200, submitted.text
        ident = UUID(submitted.json()["id"])
        path = f"{PREFIX}/approvals/{ident}/approve"
        first = await client.post(
            path, json=reason, headers={**approver, "Prefer": "respond-async"}
        )
        assert first.status_code == 202, first.text
        async with db.begin() as conn:
            if code == "SYNTHETIC_DEFINITIVE_NO_EFFECT":
                await execute(
                    conn,
                    """
                    INSERT INTO execution_attempts(execution_id,generation,worker_id,kind,claimed_at,finished_at,outcome)
                    VALUES (:id,1,gen_random_uuid(),'VERIFY',now(),now(),'FAILED')
                """,
                    id=ident,
                )
                await execute(
                    conn,
                    """
                    INSERT INTO execution_receipts(execution_id,generation,outcome,evidence_source,external_reference,evidence_digest)
                    VALUES (:id,1,'FAILED','LOOKUP',:reference,decode(repeat('ab',32),'hex'))
                """,
                    id=ident,
                    reference=f"fixture:{ident}",
                )
            await execute(
                conn,
                """
                UPDATE execution_commands SET state='FAILED',generation=:generation WHERE execution_id=:id
            """,
                id=ident,
                generation=int(code == "SYNTHETIC_DEFINITIVE_NO_EFFECT"),
            )
            await execute(
                conn,
                "UPDATE executions SET status='FAILED',error_code=:code WHERE id=:id",
                id=ident,
                code=code,
            )
        retry = await client.post(
            path, json=reason, headers={**approver, "Prefer": "respond-async"}
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["approval_status"] == "APPROVED"
        assert retry.json()["status"] == retry.json()["phase"] == "FAILED"
        assert retry.json()["error_code"] == code
        assert retry.json()["id"] == retry.json()["approval_id"] == str(ident)
        assert (
            await client.post(path, json={"reason": "changed"}, headers=approver)
        ).status_code == 409
        opposite = path.removesuffix("approve") + "reject"
        assert (
            await client.post(opposite, json=reason, headers=approver)
        ).status_code == 409
        async with db.begin() as conn:
            [counts] = await rows(
                conn,
                """
                SELECT (SELECT count(*) FROM execution_approval_decisions WHERE execution_id=:id) AS decisions,
                       (SELECT count(*) FROM execution_attempts WHERE execution_id=:id) AS attempts,
                       (SELECT count(*) FROM audit_events WHERE resource_id=:id AND action='DURABLE_APPROVAL_APPROVED') AS audit
            """,
                id=ident,
            )
            assert counts == {
                "decisions": 1,
                "attempts": int(code == "SYNTHETIC_DEFINITIVE_NO_EFFECT"),
                "audit": 1,
            }
            await execute(
                conn, "UPDATE executions SET status='SUCCEEDED' WHERE id=:id", id=ident
            )
        inconsistent = await client.post(path, json=reason, headers=approver)
        assert inconsistent.status_code == 503
        assert inconsistent.json()["error"]["code"] == "EXECUTION_INTENT_MISMATCH"
        async with db.begin() as conn:
            await execute(
                conn, "UPDATE executions SET status='FAILED' WHERE id=:id", id=ident
            )


def test_public_protected_admission_across_api_processes(sandbox):
    url, env, _ = sandbox
    before = asyncio.run(snapshot(url))
    migrate(env, "upgrade", "0012")
    assert asyncio.run(snapshot(url)) == before

    async def check():
        url_parts = make_url(env["TOOLMESH_MIGRATION_URL"])
        db = create_async_engine(url_parts, hide_parameters=True)
        tokens = {
            actor: "s" + urandom(21).hex()
            for actor in (
                "finance-agent",
                "refund-approver",
                "support-agent",
                "it-agent",
            )
        }
        wrapping, lookup, intent = [urandom(32).hex() for _ in range(3)]

        def connection(username, password):
            return url_parts.set(username=username, password=password).render_as_string(
                hide_password=False
            )

        settings = {
            **os.environ,
            "TOOLMESH_DATABASE_URL": connection("toolmesh_app", "synthetic-app-test"),
            "TOOLMESH_REDIS_URL": "redis://127.0.0.1:1",
            "TOOLMESH_PROTECTED_DATABASE_URL": connection(
                "protected_api_test", "fixture-protected-api"
            ),
            "TOOLMESH_PROTECTED_WRAPPING_KEYS": f"1:{wrapping}",
            "TOOLMESH_PROTECTED_DIGEST_KEYS": f"{lookup}:{intent}",
            "TOOLMESH_PROTECTED_WAIT_SECONDS": "0.35",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            async with db.begin() as conn:
                await execute(
                    conn,
                    "CREATE ROLE protected_api_test LOGIN PASSWORD 'fixture-protected-api'",
                )
                await execute(
                    conn, "GRANT toolmesh_protected_api TO protected_api_test"
                )
                await execute(
                    conn,
                    "UPDATE tools SET active_version=1,lifecycle='published' WHERE id IN ('payments.issue_refund','it.restart_service')",
                )
                for name, token in tokens.items():
                    await execute(
                        conn,
                        "INSERT INTO sessions VALUES (:digest,:actor,now()+interval '1 hour')",
                        digest=digest(token),
                        actor=name,
                    )
            restricted = create_async_engine(
                settings["TOOLMESH_PROTECTED_DATABASE_URL"], hide_parameters=True
            )
            try:
                async with restricted.connect() as conn:
                    assert await rows(conn, "SELECT current_user")
                for statement in (
                    "UPDATE execution_dispatch_gate SET mode='DURABLE'",
                    "DELETE FROM execution_attempts",
                    "DELETE FROM execution_receipts",
                ):
                    with pytest.raises(DBAPIError) as error:
                        async with restricted.begin() as conn:
                            await execute(conn, statement)
                    assert error.value.orig.sqlstate == "42501"
            finally:
                await restricted.dispose()
            requester, approver, outsider, it_agent = map(auth, tokens.values())
            body = {
                "tool_id": REQUEST.tool_id,
                "arguments": REQUEST.arguments,
                "idempotency_key": uuid4().hex,
            }
            async with AsyncExitStack() as stack:
                first, origin, _ = await stack.enter_async_context(api(settings))
                second, _, interrupted = await stack.enter_async_context(
                    api(
                        {
                            **settings,
                            "TOOLMESH_TEST_EXIT_AFTER_APPROVAL": "1",
                        }
                    )
                )
                legacy = await first.post(
                    f"{PREFIX}/executions",
                    json={**body, "idempotency_key": uuid4().hex},
                    headers={**requester, "Prefer": "respond-async"},
                )
                assert legacy.status_code == 200, legacy.text
                assert legacy.json()["status"] == "PENDING_APPROVAL"
                assert (
                    "phase" not in legacy.json()
                    and "Preference-Applied" not in legacy.headers
                )
                await set_mode(db, "PAUSED")
                response = await first.post(
                    f"{PREFIX}/executions", json=body, headers=requester
                )
                assert response.status_code == 503, response.text
                await set_mode(db, "DURABLE")
                async with api(
                    {
                        key: value
                        for key, value in settings.items()
                        if not key.startswith("TOOLMESH_PROTECTED_")
                    }
                ) as (ordinary, _, _):
                    response = await ordinary.post(
                        f"{PREFIX}/executions", json=body, headers=requester
                    )
                    assert response.status_code == 503

                replies = await asyncio.gather(
                    *(
                        (first if index % 2 else second).post(
                            f"{PREFIX}/executions",
                            json=body,
                            headers={
                                **requester,
                                "Prefer": "respond-async",
                            },
                        )
                        for index in range(32)
                    )
                )
                assert all(reply.status_code == 200 for reply in replies), [
                    (r.status_code, r.text) for r in replies
                ]
                [ident] = {reply.json()["id"] for reply in replies}
                pending = replies[0].json()
                assert pending == {
                    "id": ident,
                    "status": "PENDING_APPROVAL",
                    "phase": "PENDING_APPROVAL",
                    "approval_status": "PENDING",
                    "approval_id": ident,
                    "result": None,
                    "error_code": None,
                    "retry_after_seconds": None,
                }
                assert all(
                    "Preference-Applied" not in reply.headers for reply in replies
                )
                async with db.connect() as conn:
                    [count] = await rows(
                        conn,
                        """
                        SELECT (SELECT count(*) FROM execution_commands) AS commands,
                               (SELECT count(*) FROM execution_request_keys) AS reservations,
                               (SELECT count(*) FROM approvals WHERE request_key LIKE 'm9-sealed:%') AS approvals
                    """,
                    )
                    assert count == {"commands": 1, "reservations": 1, "approvals": 1}

                async with db.begin() as conn:
                    await execute(
                        conn,
                        "CREATE ROLE inherited_app_test LOGIN PASSWORD 'fixture-inherited-app'",
                    )
                    await execute(conn, "GRANT toolmesh_app TO inherited_app_test")
                legacy_id = UUID(legacy.json()["id"])
                for login in (
                    settings["TOOLMESH_DATABASE_URL"],
                    connection("inherited_app_test", "fixture-inherited-app"),
                ):
                    await assert_shared_row_boundary(login, UUID(ident), legacy_id)

                changed = {
                    **body,
                    "arguments": {**body["arguments"], "amount_minor": 124},
                }
                assert (
                    await first.post(
                        f"{PREFIX}/executions", json=changed, headers=requester
                    )
                ).status_code == 409
                decision_path = f"{PREFIX}/approvals/{ident}/approve"
                result_path = f"{PREFIX}/executions/{ident}/result"
                reason = {"reason": "fixture authorized approval"}
                assert (
                    await first.post(decision_path, json=reason, headers=outsider)
                ).status_code == 404
                for headers in (outsider, approver, it_agent):
                    denied = await first.get(result_path, headers=headers)
                    missing = await first.get(
                        f"{PREFIX}/executions/{uuid4()}/result", headers=headers
                    )
                    assert denied.status_code == missing.status_code == 404
                    assert (
                        denied.json()["error"]["code"]
                        == missing.json()["error"]["code"]
                        == "EXECUTION_NOT_AVAILABLE"
                    )

                listing = await first.get(f"{PREFIX}/approvals", headers=approver)
                assert listing.status_code == 200, listing.text
                [listed] = [value for value in listing.json() if value["id"] == ident]
                assert listed["transaction_id"] == body["arguments"]["transaction_id"]
                assert "ciphertext" not in listed and "wrapped_data_key" not in listed

                with pytest.raises(httpx.RemoteProtocolError):
                    await second.post(decision_path, json=reason, headers=approver)
                assert await interrupted.wait() == 47
                now = monotonic()
                retry = await first.post(decision_path, json=reason, headers=approver)
                assert 0.30 <= monotonic() - now < 3
                assert retry.status_code == 200, retry.text
                assert retry.json()["phase"] == "READY"
                accepted = await first.post(
                    decision_path,
                    json=reason,
                    headers={**approver, "Prefer": "respond-async"},
                )
                assert accepted.status_code == 202
                assert accepted.headers["location"] == f"{PREFIX}/executions/{ident}"
                assert accepted.headers["retry-after"] == "1"
                assert accepted.headers["preference-applied"] == "respond-async"
                assert accepted.json() == retry.json()
                async with Client(
                    f"{origin}/mcp/", auth=tokens["finance-agent"], timeout=8
                ) as mcp:
                    retrieved = await mcp.call_tool(
                        "get_execution_result", {"execution_id": ident}
                    )
                    assert retrieved.data["phase"] == "READY"
                    assert retrieved.data["result_availability"] == "NOT_READY"
                    invocation = await mcp.call_tool(
                        "invoke_tool", {**body, "respond_async": True}
                    )
                    assert invocation.data == retrieved.data
                    with pytest.raises(ToolError, match="EXECUTION_NOT_AVAILABLE"):
                        await mcp.call_tool(
                            "get_execution_result", {"execution_id": str(uuid4())}
                        )

                async with db.connect() as conn:
                    [state] = await rows(
                        conn,
                        """
                        SELECT state,
                          (SELECT count(*) FROM execution_approval_decisions) AS decisions,
                          (SELECT count(*) FROM execution_attempts) AS attempts,
                          (SELECT count(*) FROM execution_receipts) AS receipts
                        FROM execution_commands WHERE execution_id=:id
                    """,
                        id=UUID(ident),
                    )
                    assert state == {
                        "state": "READY",
                        "decisions": 1,
                        "attempts": 0,
                        "receipts": 0,
                    }
                payloads = PayloadProtection(
                    SandboxWrappingKeys({1: bytes.fromhex(wrapping)}, 1)
                )
                store = CommandStore(
                    db,
                    payloads,
                    CommandDigests(bytes.fromhex(lookup), bytes.fromhex(intent)),
                )
                async with db.begin() as conn:
                    await execute(
                        conn,
                        "INSERT INTO workspaces (id,display_name) VALUES ('other','Other')",
                    )
                foreign = await store.submit(
                    replace(REQUEST, workspace_id="other", idempotency_key=uuid4().hex)
                )
                assert (
                    await first.get(
                        f"{PREFIX}/executions/{foreign.execution_id}/result",
                        headers=requester,
                    )
                ).status_code == 404
                assert (
                    await first.post(
                        f"{PREFIX}/approvals/{foreign.execution_id}/approve",
                        json=reason,
                        headers=approver,
                    )
                ).status_code == 404

                alien = uuid4()
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        INSERT INTO executions(id,principal,tool_id,version,status)
                        VALUES (:alien,'finance-agent','payments.issue_refund',1,'PENDING_APPROVAL')
                    """,
                        alien=alien,
                    )
                    await execute(
                        conn,
                        """
                        INSERT INTO approvals(id,requester,tool_id,version,customer_id,arguments,intent_hash,request_key,status)
                        VALUES (:alien,'finance-agent','payments.issue_refund',1,'C-999','{}','fixture',:key,'PENDING')
                    """,
                        alien=alien,
                        key=uuid4().hex,
                    )
                    await execute(
                        conn,
                        """
                        INSERT INTO execution_commands(execution_id,workspace_id,requester,customer_id,tool_id,tool_version,
                                                       contract_digest,state,ciphertext,nonce,wrapped_data_key,key_version)
                        SELECT :alien,workspace_id,requester,'C-999',tool_id,tool_version,
                               contract_digest,'PENDING_APPROVAL',ciphertext,nonce,wrapped_data_key,key_version
                        FROM execution_commands WHERE execution_id=:id
                    """,
                        alien=alien,
                        id=UUID(ident),
                    )
                    await execute(
                        conn,
                        "UPDATE executions SET status='UNKNOWN' WHERE id=:id",
                        id=UUID(ident),
                    )
                    await execute(
                        conn,
                        """
                        UPDATE execution_commands SET state='UNKNOWN',next_check_at=now()
                        WHERE execution_id=:id
                    """,
                        id=UUID(ident),
                    )
                for hidden in (alien, foreign.execution_id):
                    for suffix in ("", "/events", "/result"):
                        denied = await first.get(
                            f"{PREFIX}/executions/{hidden}{suffix}", headers=requester
                        )
                        assert denied.status_code == 404, denied.text
                wrong_customer = await first.post(
                    f"{PREFIX}/approvals/{alien}/approve", json=reason, headers=approver
                )
                assert wrong_customer.status_code == 404, wrong_customer.text
                orphan = uuid4()
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        INSERT INTO audit_events(actor,action,resource_id)
                        SELECT 'foreign-actor','DURABLE_APPROVAL_REQUESTED',
                               CASE WHEN n % 2=0 THEN CAST(:foreign AS uuid) ELSE CAST(:alien AS uuid) END
                        FROM generate_series(1,110) AS n
                    """,
                        foreign=foreign.execution_id,
                        alien=alien,
                    )
                    await execute(
                        conn,
                        """
                        INSERT INTO audit_events(actor,action,resource_id)
                        VALUES ('orphan-actor','DURABLE_APPROVAL_REQUESTED',:orphan)
                    """,
                        orphan=orphan,
                    )
                audit_url = f"{PREFIX}/audit-events"
                audit = await first.get(audit_url, headers=approver)
                assert audit.status_code == 200, audit.text
                visible = {entry["resource_id"] for entry in audit.json()}
                assert {ident, str(legacy_id)} <= visible
                assert not visible & {
                    str(alien),
                    str(foreign.execution_id),
                    str(orphan),
                }
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        UPDATE execution_approval_entitlements SET enabled=false
                        WHERE principal='refund-approver' AND tool_id='payments.issue_refund'
                    """,
                    )
                audit_after_revoke = await first.get(audit_url, headers=approver)
                assert audit_after_revoke.status_code == 200
                visible = {entry["resource_id"] for entry in audit_after_revoke.json()}
                assert str(legacy_id) in visible and ident not in visible
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        UPDATE execution_approval_entitlements SET enabled=true
                        WHERE principal='refund-approver' AND tool_id='payments.issue_refund'
                    """,
                    )
                unknown = await first.post(
                    f"{PREFIX}/executions",
                    json=body,
                    headers={**requester, "Prefer": "respond-async"},
                )
                assert unknown.status_code == 200, unknown.text
                assert unknown.json()["status"] == unknown.json()["phase"] == "UNKNOWN"
                assert unknown.json()["error_code"] == "UNKNOWN_OUTCOME"
                assert unknown.json()["retry_after_seconds"] is None
                assert unknown.json()["next_check_at"]
                assert (
                    "retry-after" not in unknown.headers
                    and "preference-applied" not in unknown.headers
                )
                detail = await first.get(
                    f"{PREFIX}/executions/{ident}", headers=requester
                )
                assert detail.json()["phase"] == "UNKNOWN"
                assert detail.json()["approval_status"] == "APPROVED"

                scope = ProtectedIntentScope(
                    workspace_id=REQUEST.workspace_id,
                    requester=REQUEST.requester,
                    customer_id=REQUEST.arguments["customer_id"],
                    tool_id=REQUEST.tool_id,
                )
                result = {
                    "id": ident,
                    **REQUEST.arguments,
                    "status": "refunded",
                    "private_fixture_field": "must not be exposed",
                }
                sealed = payloads.seal(
                    canonical_json(result),
                    PayloadContext("receipt", UUID(ident), scope),
                )
                async with db.begin() as conn:
                    await execute(
                        conn,
                        """
                        INSERT INTO execution_attempts(execution_id,generation,worker_id,kind,claimed_at,finished_at,outcome)
                        VALUES (:id,1,gen_random_uuid(),'VERIFY',now(),now(),'SUCCEEDED')
                    """,
                        id=UUID(ident),
                    )
                    await execute(
                        conn,
                        """
                        INSERT INTO execution_receipts(execution_id,generation,outcome,evidence_source,external_reference,
                                                       evidence_digest,ciphertext,wrapped_data_key,nonce,key_version)
                        VALUES (:id,1,'SUCCEEDED','LOOKUP',:reference,decode(repeat('ab',32),'hex'),
                                :ciphertext,:wrapped_data_key,:nonce,:key_version)
                    """,
                        id=UUID(ident),
                        reference=f"fixture:{ident}",
                        **sealed.model_dump(),
                    )
                    await execute(
                        conn,
                        "UPDATE executions SET status='SUCCEEDED' WHERE id=:id",
                        id=UUID(ident),
                    )
                    await execute(
                        conn,
                        "UPDATE execution_commands SET state='SUCCEEDED',generation=1,next_check_at=NULL WHERE execution_id=:id",
                        id=UUID(ident),
                    )
                projected = await first.get(
                    result_path, params={"fields": "amount_minor"}, headers=requester
                )
                assert projected.status_code == 200, projected.text
                assert projected.json()["result"] == {"amount_minor": 123}
                assert projected.json()["result_availability"] == "AVAILABLE"
                assert (
                    await first.get(
                        result_path,
                        params={"fields": "private_fixture_field"},
                        headers=requester,
                    )
                ).status_code == 422
                assert (
                    await first.get(result_path, headers=approver)
                ).status_code == 404

                succeeded = await first.post(
                    decision_path, json=reason, headers=approver
                )
                assert (
                    succeeded.status_code == 200
                    and succeeded.json()["status"] == "SUCCEEDED"
                )
                await assert_failed_approval_retries(
                    first, db, body, requester, approver
                )

                # Existing workflows enter the same protected boundary without
                # requiring the unimplemented transactional step-link feature.
                guided_id, scripted_id = uuid4(), uuid4()
                async with db.begin() as conn:
                    await execute(
                        conn,
                        "UPDATE tools SET active_version=1,lifecycle='published' WHERE id='payments.get_transaction'",
                    )
                    await execute(
                        conn,
                        """
                        INSERT INTO duplicate_workflows(id,customer_id,order_id,started_by,evidence,status)
                        VALUES (:id,'C-102','order-fixture','support-agent',
                                '{"groups":[["fixture-transaction"]]}'::jsonb,'TICKET_OPEN')
                    """,
                        id=guided_id,
                    )
                    await execute(
                        conn,
                        """
                        INSERT INTO agent_runs(id,principal,profile,arguments,next_step)
                        VALUES (:id,'it-agent','it','{"service_id":"fixture-service"}',3)
                    """,
                        id=scripted_id,
                    )
                guided_path = f"{PREFIX}/workflows/{guided_id}/refund"
                guided_input = {"transaction_id": "fixture-transaction"}
                guided = await first.post(
                    guided_path, json=guided_input, headers=requester
                )
                assert guided.status_code == 200, guided.text
                assert guided.json()["refund_status"] == "PENDING_APPROVAL"
                repeated = await first.post(
                    guided_path, json=guided_input, headers=requester
                )
                assert repeated.status_code == 200, repeated.text
                assert repeated.json()["refund_id"] == guided.json()["refund_id"]
                async with asyncio.timeout(8):
                    while True:
                        scripted = await first.get(
                            f"{PREFIX}/playground/runs/{scripted_id}", headers=it_agent
                        )
                        assert scripted.status_code == 200, scripted.text
                        if scripted.json()["status"] == "WAITING_FOR_APPROVAL":
                            assert scripted.json()["approval_id"]
                            break
                        await asyncio.sleep(0.1)
                async with db.begin() as conn:
                    await execute(
                        conn,
                        "UPDATE refund_grants SET enabled=false WHERE principal='finance-agent'",
                    )
                assert (
                    await first.get(result_path, headers=requester)
                ).status_code == 404
        finally:
            await set_mode(db, "LEGACY")
            await db.dispose()

    asyncio.run(check())


def test_protected_startup_checks_effective_identity(sandbox):
    from gateway.app import create_app
    from gateway.config import Settings

    url, env, _ = sandbox
    migrate(env, "upgrade", "0012")

    async def check():
        root_url = make_url(env["TOOLMESH_MIGRATION_URL"])
        ordinary_url = root_url.set(
            username="toolmesh_app", password="synthetic-app-test"
        )
        db = create_async_engine(root_url, hide_parameters=True)
        token = "s" + urandom(21).hex()
        try:
            async with db.begin() as conn:
                await execute(conn, "CREATE ROLE startup_inherited_extra NOLOGIN")
                await execute(
                    conn,
                    "GRANT UPDATE (outcome) ON execution_receipts TO startup_inherited_extra",
                )
                for login in (
                    "startup_allowed",
                    "startup_inherited",
                    "startup_direct",
                    "startup_callable",
                ):
                    await execute(
                        conn,
                        f"CREATE ROLE {login} LOGIN PASSWORD 'fixture-startup-login'",
                    )
                    await execute(conn, f"GRANT toolmesh_protected_api TO {login}")
                await execute(
                    conn, "GRANT startup_inherited_extra TO startup_inherited"
                )
                await execute(
                    conn,
                    "GRANT UPDATE (mode) ON execution_dispatch_gate TO startup_direct",
                )
                await execute(
                    conn,
                    """
                    CREATE FUNCTION public.fixture_restricted_privilege() RETURNS integer
                    LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS 'SELECT 1'
                """,
                )
                await execute(
                    conn,
                    "REVOKE EXECUTE ON FUNCTION public.fixture_restricted_privilege() FROM PUBLIC",
                )
                await execute(
                    conn,
                    "GRANT EXECUTE ON FUNCTION public.fixture_restricted_privilege() TO startup_callable",
                )
                await execute(
                    conn,
                    """
                    INSERT INTO sessions VALUES (:digest,'finance-agent',now()+interval '1 hour')
                """,
                    digest=digest(token),
                )
                await execute(
                    conn,
                    "UPDATE tools SET active_version=1,lifecycle='published' WHERE id='payments.issue_refund'",
                )
                await execute(
                    conn,
                    "UPDATE refund_grants SET enabled=true WHERE principal='finance-agent'",
                )
            base = {
                "database_url": ordinary_url.render_as_string(hide_password=False),
                "redis_url": "redis://127.0.0.1:1",
            }
            wrapping, lookup, intent = [urandom(32).hex() for _ in range(3)]
            keys = {
                "protected_wrapping_keys": f"1:{wrapping}",
                "protected_digest_keys": f"{lookup}:{intent}",
            }
            for bad_url in (ordinary_url, root_url.set(database="wrong_database")):
                with pytest.raises(
                    ValueError, match="own login on the gateway database"
                ):
                    create_app(
                        Settings(
                            **base,
                            **keys,
                            protected_database_url=bad_url.render_as_string(
                                hide_password=False
                            ),
                        )
                    )
            request = {
                "tool_id": REQUEST.tool_id,
                "arguments": REQUEST.arguments,
                "idempotency_key": uuid4().hex,
            }
            for login in (
                "startup_allowed",
                "startup_inherited",
                "startup_direct",
                "startup_callable",
                "toolmesh_migrator",
                None,
            ):
                configured = dict(base)
                if login is not None:
                    target = (
                        root_url
                        if login == "toolmesh_migrator"
                        else root_url.set(
                            username=login,
                            password="fixture-startup-login",
                        )
                    )
                    configured.update(
                        keys,
                        protected_database_url=target.render_as_string(
                            hide_password=False
                        ),
                    )
                settings = {
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    **{
                        f"TOOLMESH_{key.upper()}": value
                        for key, value in configured.items()
                    },
                }
                await set_mode(db, "LEGACY")
                async with api(settings) as (client, _, _):
                    eligible = login in {"startup_allowed", None}
                    readiness = await client.get("/readyz")
                    assert readiness.json()["checks"]["postgres"] == (
                        "available" if eligible else "unavailable"
                    )
                    await set_mode(db, "DURABLE")
                    response = await client.post(
                        f"{PREFIX}/executions", json=request, headers=auth(token)
                    )
                    assert response.status_code == (
                        200 if login == "startup_allowed" else 503
                    ), response.text
                    readiness = await client.get("/readyz")
                    assert (
                        readiness.status_code == 503
                    )  # The fixture intentionally has no Redis.
                    assert readiness.json()["checks"]["postgres"] == (
                        "available" if login == "startup_allowed" else "unavailable"
                    )
            async with db.begin() as conn:
                await execute(conn, "ALTER ROLE toolmesh_protected_api BYPASSRLS")
            target = root_url.set(
                username="startup_allowed", password="fixture-startup-login"
            )
            elevated_group = {
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                **{
                    f"TOOLMESH_{key.upper()}": value
                    for key, value in {
                        **base,
                        **keys,
                        "protected_database_url": target.render_as_string(
                            hide_password=False
                        ),
                    }.items()
                },
            }
            await set_mode(db, "DURABLE")
            async with api(elevated_group) as (client, _, _):
                assert (await client.get("/readyz")).json()["checks"][
                    "postgres"
                ] == "unavailable"
                response = await client.post(
                    f"{PREFIX}/executions", json=request, headers=auth(token)
                )
                assert response.status_code == 503, response.text
        finally:
            await set_mode(db, "LEGACY")
            await db.dispose()

    asyncio.run(check())
