import asyncio
import json
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.approvals import Approvals, event
from gateway.identity import Principal
from shared.contracts import TOOLS, tool_contract
from shared.database import execute, rows

CATALOG = """
    SELECT t.id, t.service_id, t.active_version AS version, v.contract
    FROM tools t JOIN tool_versions v ON (v.tool_id, v.version) = (t.id, t.active_version)
    WHERE t.service_id = ANY(CAST(:services AS text[]))
"""


class Runtime:
    def __init__(
        self,
        db: AsyncEngine,
        redis: Redis,
        payments_secret: str,
        support_secret: str,
        communication_secret: str = "",
    ):
        self.db = db
        self.redis = redis
        self.payments_secret = payments_secret
        self.support_secret = support_secret
        self.communication_secret = communication_secret
        self.approvals = Approvals(db, self.downstream)

    async def tools(
        self,
        principal: Principal,
        service: str | None = None,
        query: str = "",
        limit: int = 20,
    ):
        async with self.db.connect() as conn:
            return await rows(
                conn,
                CATALOG
                + """
                AND ((v.contract->>'risk' = 'read') OR (v.contract->>'risk' = 'write' AND :can_write)
                    OR (v.contract->>'risk' = 'approval' AND EXISTS (
                        SELECT 1 FROM refund_grants WHERE principal=:principal AND enabled AND customer_id=:customer)))
                AND (:service = '' OR t.service_id = :service)
                AND (strpos(lower(t.id || ' ' || (v.contract->>'description')), lower(:query)) > 0
                     OR :query = '')
                ORDER BY CASE WHEN strpos(lower(t.id), lower(:query)) > 0 THEN 0 ELSE 1 END, t.id
                LIMIT :limit
            """,
                services=list(principal.services),
                can_write=principal.can_write,
                principal=principal.id,
                customer=principal.customer,
                service=service or "",
                query=query,
                limit=limit,
            )

    async def servers(self, principal: Principal, query: str, limit: int):
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT s.id, s.name, s.protocol FROM services s
                WHERE s.id = ANY(CAST(:services AS text[]))
                  AND (strpos(lower(s.name || ' ' || s.id), lower(:query)) > 0 OR :query = '')
                  AND EXISTS (SELECT 1 FROM tools t JOIN tool_versions v
                    ON (v.tool_id, v.version) = (t.id, t.active_version)
                    WHERE t.service_id=s.id AND ((v.contract->>'risk' = 'read') OR (v.contract->>'risk' = 'write' AND :can_write)
                    OR (v.contract->>'risk' = 'approval' AND EXISTS (
                        SELECT 1 FROM refund_grants WHERE principal=:principal AND enabled AND customer_id=:customer))))
                ORDER BY s.id LIMIT :limit
            """,
                services=list(principal.services),
                query=query,
                limit=limit,
                can_write=principal.can_write,
                principal=principal.id,
                customer=principal.customer,
            )

    async def schema(self, principal: Principal, tool_id: str, version: int | None):
        async with self.db.connect() as conn:
            found = await rows(
                conn,
                CATALOG
                + """
                AND t.id=:id AND (CAST(:version AS integer) IS NULL OR t.active_version=CAST(:version AS integer))
                AND ((v.contract->>'risk' = 'read') OR (v.contract->>'risk' = 'write' AND :can_write)
                    OR (v.contract->>'risk' = 'approval' AND EXISTS (
                        SELECT 1 FROM refund_grants WHERE principal=:principal AND enabled AND customer_id=:customer)))
            """,
                services=list(principal.services),
                id=tool_id,
                version=version,
                can_write=principal.can_write,
                principal=principal.id,
                customer=principal.customer,
            )
        if not found:
            raise HTTPException(404, "TOOL_NOT_AVAILABLE")
        return found[0]

    async def quota(self, principal: Principal, tool_id: str) -> None:
        try:
            async with asyncio.timeout(1):
                allowed = await self.redis.eval(
                    """
                    local result=1
                    for i,key in ipairs(KEYS) do
                        local n=redis.call('INCR',key)
                        if n==1 then redis.call('EXPIRE',key,60) end
                        if n>tonumber(ARGV[i]) then result=0 end
                    end
                    return result
                """,
                    2,
                    "toolmesh:global",
                    f"toolmesh:invoke:{principal.id}:{tool_id}",
                    300,
                    30,
                )
        except (TimeoutError, RedisError):
            raise HTTPException(503, "QUOTA_UNAVAILABLE") from None
        if not allowed:
            raise HTTPException(429, "RATE_LIMITED")

    async def downstream(
        self, tool_id: str, args: dict, *, refund_id: UUID | None = None
    ) -> dict:
        service, operation = tool_id.split(".", 1)
        async with asyncio.timeout(5):
            if service == "payments":
                if operation == "issue_refund" and refund_id is None:
                    raise HTTPException(403, "APPROVAL_REQUIRED")
                extra = {"X-Refund-ID": str(refund_id)} if refund_id is not None else {}
                async with httpx.AsyncClient(
                    follow_redirects=False,
                    trust_env=False,
                    timeout=3,
                    headers={
                        "Authorization": f"Bearer {self.payments_secret}",
                        "Accept-Encoding": "identity",
                        **extra,
                    },
                ) as client:
                    async with client.stream(
                        "POST", f"http://payments:8000/{operation}", json=args
                    ) as response:
                        if response.status_code == 404:
                            raise HTTPException(404, "RECORD_NOT_AVAILABLE")
                        if response.status_code == 409:
                            raise HTTPException(409, "REFUND_CONFLICT")
                        response.raise_for_status()
                        if (
                            response.headers.get("content-encoding", "identity")
                            != "identity"
                        ):
                            raise HTTPException(502, "UPSTREAM_ERROR")
                        buffer = bytearray()
                        async for chunk in response.aiter_bytes(8192):
                            buffer.extend(chunk)
                            if len(buffer) > 65536:
                                raise HTTPException(502, "UPSTREAM_RESPONSE_LIMIT")
                return json.loads(buffer)
            if service in {"support", "communication"}:
                secret = (
                    self.support_secret
                    if service == "support"
                    else self.communication_secret
                )
                transport = StreamableHttpTransport(
                    f"http://{service}:8000/mcp",
                    auth=secret,
                    httpx_client_factory=bounded_client,
                )
                async with Client(transport, timeout=4) as client:
                    result = await client.call_tool(
                        operation, args, raise_on_error=False
                    )
                    if result.is_error:
                        if (
                            result.content
                            and getattr(result.content[0], "text", "")
                            == "RECORD_NOT_AVAILABLE"
                        ):
                            raise HTTPException(404, "RECORD_NOT_AVAILABLE")
                        raise HTTPException(502, "UPSTREAM_ERROR")
                    return result.structured_content or {}
        raise HTTPException(404, "TOOL_NOT_AVAILABLE")

    async def invoke(
        self,
        principal: Principal,
        tool_id: str,
        arguments: dict,
        version: int | None = None,
        fields: list[str] | None = None,
        idempotency_key: str | None = None,
    ):
        trace = uuid4()
        selected = None
        result = None
        code = None
        status = "DENIED"
        write = False
        async with self.db.begin() as conn:
            await execute(
                conn,
                """
                INSERT INTO executions (id, principal, status) VALUES (:id, :principal, 'RECEIVED')
            """,
                id=trace,
                principal=principal.id,
            )
        try:
            selected = await self.schema(principal, tool_id, version)
            _, input_model, output_model, write = TOOLS[tool_id]
            if fields and not set(fields).issubset(output_model.model_fields):
                raise HTTPException(422, "INVALID_ARGUMENT")
            if selected["contract"] != tool_contract(tool_id):
                raise HTTPException(409, "VERSION_INCOMPATIBLE")
            try:
                args = input_model.model_validate_json(
                    json.dumps(arguments)
                ).model_dump(mode="json")
            except (ValidationError, ValueError, RecursionError):
                raise HTTPException(422, "INVALID_ARGUMENT") from None
            if args["customer_id"] != principal.customer:
                raise HTTPException(404, "TOOL_NOT_AVAILABLE")
            await self.quota(principal, tool_id)
            await self.schema(principal, tool_id, selected["version"])
            if tool_id == "payments.issue_refund":
                if not idempotency_key:
                    raise HTTPException(422, "IDEMPOTENCY_KEY_REQUIRED")
                queued = await self.approvals.queue(
                    trace,
                    principal,
                    tool_id,
                    selected["version"],
                    args,
                    idempotency_key,
                )
                status = "QUEUED"
                return queued
            if tool_id in {"communication.send_notification", "support.resolve_ticket"}:
                await self.confirm_refund(tool_id, args)
            async with self.db.begin() as conn:
                await execute(
                    conn,
                    "UPDATE executions SET status='RUNNING', tool_id=:tool, version=:version WHERE id=:id",
                    id=trace,
                    tool=tool_id,
                    version=selected["version"],
                )
            status = "RUNNING"
            payload = await self.downstream(tool_id, args)
            result = output_model.model_validate_json(json.dumps(payload)).model_dump(
                mode="json"
            )
            if fields:
                result = {field: result[field] for field in fields}
            status = "SUCCEEDED"
        except HTTPException as exc:
            code = str(exc.detail)
        except (TimeoutError, httpx.TimeoutException):
            code = "UPSTREAM_TIMEOUT"
        except (
            ToolError,
            httpx.HTTPError,
            ValueError,
            RuntimeError,
            ExceptionGroup,
        ):
            code = "UPSTREAM_ERROR"
        finally:
            if status == "RUNNING" and code is not None:
                status = "UNKNOWN" if write else "FAILED"
            if status not in {"RUNNING", "QUEUED"}:
                if code == "TOOL_NOT_AVAILABLE":
                    selected = None
                async with self.db.begin() as conn:
                    await execute(
                        conn,
                        """
                        UPDATE executions SET tool_id=:tool, version=:version, status=:status,
                            error_code=:code, finished_at=now()
                        WHERE id=:id
                    """,
                        id=trace,
                        tool=tool_id if selected else None,
                        version=selected["version"] if selected else None,
                        status=status,
                        code=code,
                    )
                    if status == "SUCCEEDED" and tool_id in {
                        "communication.send_notification",
                        "support.resolve_ticket",
                    }:
                        action = (
                            "NOTIFICATION_SIMULATED"
                            if tool_id.startswith("communication.")
                            else "TICKET_RESOLVED"
                        )
                        await event(conn, principal.id, action, trace)
        return {
            "id": str(trace),
            "status": status,
            "error_code": code,
            "result": result,
        }

    async def confirm_refund(self, tool_id: str, arguments: dict):
        lookup = {
            "customer_id": arguments["customer_id"],
            "refund_id": arguments["refund_id"],
        }
        try:
            refund = await self.downstream("payments.get_refund_status", lookup)
            if (
                refund.get("status") != "SUCCEEDED"
                or refund.get("id") != arguments["refund_id"]
            ):
                raise HTTPException(409, "REFUND_NOT_CONFIRMED")
            if tool_id == "support.resolve_ticket":
                ticket = await self.downstream(
                    "support.get_ticket",
                    {
                        "customer_id": arguments["customer_id"],
                        "ticket_id": arguments["ticket_id"],
                    },
                )
                capture = await self.downstream(
                    "payments.get_transaction",
                    {
                        "customer_id": arguments["customer_id"],
                        "transaction_id": refund["transaction_id"],
                    },
                )
                if ticket["order_id"] != capture["order_id"]:
                    raise HTTPException(409, "TICKET_REFUND_MISMATCH")
                await self.downstream("communication.get_notification", lookup)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise HTTPException(
                    409, "REFUND_OR_NOTIFICATION_NOT_CONFIRMED"
                ) from None
            raise

    async def executions(
        self,
        principal: Principal,
        trace: UUID | None = None,
        hours: int | None = None,
        status: str | None = None,
    ):
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT id, principal, tool_id, version, status, error_code, started_at, finished_at
                FROM executions
                WHERE (principal=:principal OR :admin)
                  AND (CAST(:trace AS uuid) IS NULL OR id=CAST(:trace AS uuid))
                  AND (CAST(:hours AS integer) IS NULL OR started_at>=now()-make_interval(hours=>:hours))
                  AND (CAST(:status AS text) IS NULL OR status=:status)
                ORDER BY started_at DESC, id LIMIT 100
            """,
                principal=principal.id,
                admin=principal.can_publish or principal.can_approve,
                trace=trace,
                hours=hours,
                status=status,
            )


class BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > 65536:
                raise HTTPException(502, "UPSTREAM_RESPONSE_LIMIT")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


def bounded_client(
    headers=None, timeout=None, auth=None, **_options
) -> httpx.AsyncClient:
    async def limit(response: httpx.Response):
        if not isinstance(response.stream, httpx.AsyncByteStream):
            raise HTTPException(502, "UPSTREAM_ERROR")
        if response.headers.get("content-encoding", "identity") != "identity":
            raise HTTPException(502, "UPSTREAM_ERROR")
        response.stream = BoundedStream(response.stream)

    return httpx.AsyncClient(
        headers={**(headers or {}), "Accept-Encoding": "identity"},
        timeout=timeout,
        auth=auth,
        follow_redirects=False,
        trust_env=False,
        event_hooks={"response": [limit]},
    )
