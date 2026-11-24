import asyncio
import json
import time
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from control_plane.sources import resolve
from gateway.access import POLICY, object_allowed, scope_params
from gateway.approvals import Approvals, event
from gateway.discovery import Discovery, Strategy
from gateway.grpc_adapter import call_it
from gateway.identity import Principal
from gateway.protected_dispatch import PROTECTED_TOOLS, ProtectedDispatch, dispatch_mode
from gateway.redaction import redact
from gateway.resilience import ReadDispatch
from gateway.telemetry import Telemetry, request_id, trace_id
from shared.contracts import TOOLS, tool_contract
from shared.database import execute, rows

CATALOG = """
    SELECT t.id, t.service_id, t.active_version AS version, v.contract
    FROM tools t JOIN tool_versions v ON (v.tool_id, v.version) = (t.id, t.active_version)
    WHERE
"""


class Runtime:
    def __init__(
        self,
        db: AsyncEngine,
        redis: Redis,
        payments_secret: str,
        support_secret: str,
        communication_secret: str = "",
        inventory_secret: str = "",
        it_secret: str = "",
        discovery: Discovery | None = None,
        hr_secret: str = "",
        analytics_secret: str = "",
        telemetry: Telemetry | None = None,
        protected: ProtectedDispatch | None = None,
    ):
        self.db = db
        self.protected = protected
        self.redis = redis
        self.payments_secret = payments_secret
        self.support_secret = support_secret
        self.communication_secret = communication_secret
        self.inventory_secret = inventory_secret
        self.it_secret = it_secret
        self.hr_secret = hr_secret
        self.analytics_secret = analytics_secret
        self.discovery = discovery
        self.telemetry = telemetry or Telemetry()
        self.approvals = Approvals(db, self.downstream, self.telemetry)
        self.read_dispatch = ReadDispatch(self.telemetry.dispatch_event)

    async def tools(
        self,
        principal: Principal,
        service: str | None = None,
        query: str = "",
        limit: int = 20,
        strategy: Strategy = "keyword",
    ):
        if self.discovery is None:
            raise HTTPException(503, "DISCOVERY_UNAVAILABLE")
        with self.telemetry.span("discovery.tools"):
            return await self.discovery.tools(
                principal, service, query, limit, strategy
            )

    async def servers(self, principal: Principal, query: str, limit: int):
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT s.id,s.name,s.protocol FROM services s
                WHERE (strpos(lower(s.name||' '||s.id),lower(:query))>0 OR :query='')
                AND EXISTS (SELECT 1 FROM tools t JOIN tool_versions v ON (v.tool_id,v.version)=(t.id,t.active_version)
                    WHERE t.service_id=s.id AND
            """
                + POLICY
                + """) ORDER BY s.id LIMIT :limit""",
                query=query,
                limit=limit,
                **scope_params(principal),
            )

    async def schema(self, principal: Principal, tool_id: str, version: int | None):
        async with self.db.connect() as conn:
            found = await rows(
                conn,
                CATALOG
                + POLICY
                + """
                AND t.id=:id AND (CAST(:version AS integer) IS NULL OR t.active_version=CAST(:version AS integer))
            """,
                id=tool_id,
                version=version,
                **scope_params(principal),
            )
        if not found:
            raise HTTPException(404, "TOOL_NOT_AVAILABLE")
        return found[0]

    async def quota(self, principal: Principal, tool_id: str) -> None:
        async with self.db.connect() as conn:
            policy = (
                await rows(
                    conn, "SELECT per_tool,global_limit FROM quota_settings WHERE id"
                )
            )[0]
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
                    policy["global_limit"],
                    policy["per_tool"],
                )
        except (TimeoutError, RedisError):
            raise HTTPException(503, "QUOTA_UNAVAILABLE") from None
        if not allowed:
            raise HTTPException(429, "RATE_LIMITED")

    async def downstream(
        self, tool_id: str, args: dict, *, refund_id: UUID | None = None
    ) -> dict:
        with self.telemetry.span("execution.adapter"):
            return await self._downstream(tool_id, args, refund_id=refund_id)

    async def _downstream(
        self, tool_id: str, args: dict, *, refund_id: UUID | None = None
    ) -> dict:
        service, operation = tool_id.split(".", 1)
        async with asyncio.timeout(5):
            if service == "it":
                if operation == "restart_service":
                    if refund_id is None:
                        raise HTTPException(403, "APPROVAL_REQUIRED")
                    args = {**args, "operation_id": str(refund_id)}
                return await call_it(
                    await resolve("it", 50051), self.it_secret, operation, args
                )
            if service in {"payments", "inventory", "hr", "analytics"}:
                address = (
                    await resolve(service, 8000)
                    if service != "payments"
                    else "payments"
                )
                secret = {
                    "payments": self.payments_secret,
                    "inventory": self.inventory_secret,
                    "hr": self.hr_secret,
                    "analytics": self.analytics_secret,
                }[service]
                if operation == "issue_refund" and refund_id is None:
                    raise HTTPException(403, "APPROVAL_REQUIRED")
                extra = {"X-Refund-ID": str(refund_id)} if refund_id is not None else {}
                async with httpx.AsyncClient(
                    follow_redirects=False,
                    trust_env=False,
                    timeout=3,
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "Accept-Encoding": "identity",
                        **extra,
                    },
                ) as client:
                    async with client.stream(
                        "POST", f"http://{address}:8000/{operation}", json=args
                    ) as response:
                        if response.status_code == 404:
                            raise HTTPException(404, "RECORD_NOT_AVAILABLE")
                        if response.status_code in {409, 422}:
                            safe = {
                                "REFUND_CONFLICT",
                                "LEAVE_CONFLICT",
                                "INVALID_LEAVE_DATES",
                                "INVALID_REPORTING_WINDOW",
                            }
                            detail = (
                                (await response.aread())
                                if response.headers.get("content-length", "").isdigit()
                                and int(response.headers["content-length"]) < 2048
                                else b"{}"
                            )
                            code = json.loads(detail).get("detail")
                            raise HTTPException(
                                response.status_code,
                                code
                                if isinstance(code, str) and code in safe
                                else "INVALID_ARGUMENT",
                            )
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
                        message = (
                            getattr(result.content[0], "text", "")
                            if result.content
                            else ""
                        )
                        if message in {"INVALID_TICKET_TRANSITION", "CHANNEL_EXISTS"}:
                            raise HTTPException(409, message)
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
        *,
        read_only: bool = False,
        respond_async: bool = False,
    ):
        mode = await dispatch_mode(self.db) if tool_id in PROTECTED_TOOLS else "LEGACY"
        if mode == "PAUSED":
            raise HTTPException(503, "DURABLE_ADMISSION_DISABLED")
        if mode == "DURABLE":
            if read_only:
                raise HTTPException(403, "READ_ONLY_REQUIRED")
            return await self.require_protected().submit(
                principal,
                tool_id,
                arguments,
                version,
                fields,
                idempotency_key,
                respond_async,
            )
        trace = uuid4()
        started = time.monotonic()
        selected = None
        result = None
        code = None
        status = "DENIED"
        write = False
        async with self.db.begin() as conn:
            await execute(
                conn,
                """
                INSERT INTO executions (id, principal, status, request_id, otel_trace_id)
                VALUES (:id, :principal, 'RECEIVED', :request, :otel)
            """,
                id=trace,
                principal=principal.id,
                request=request_id.get(),
                otel=trace_id(),
            )
        try:
            with self.telemetry.span("execution.authorize"):
                selected = await self.schema(principal, tool_id, version)
            with self.telemetry.span("execution.validate"):
                definition = selected["contract"]
                if read_only and definition["risk"] != "read":
                    raise HTTPException(403, "READ_ONLY_REQUIRED")
                dynamic = tool_id == "inventory.get_inventory"
                input_model = output_model = None
                if dynamic:
                    if not Draft202012Validator(definition["input_schema"]).is_valid(
                        arguments
                    ):
                        raise HTTPException(422, "INVALID_ARGUMENT")
                    args = arguments
                    permitted = set(definition["output_schema"]["properties"])
                else:
                    _, input_model, output_model, write = TOOLS[tool_id]
                    if definition != tool_contract(tool_id):
                        raise HTTPException(409, "VERSION_INCOMPATIBLE")
                    permitted = set(output_model.model_fields)
                    try:
                        args = input_model.model_validate_json(
                            json.dumps(arguments)
                        ).model_dump(mode="json")
                    except (ValidationError, ValueError, RecursionError):
                        raise HTTPException(422, "INVALID_ARGUMENT") from None
                if read_only and write:
                    raise HTTPException(403, "READ_ONLY_REQUIRED")
                if fields and not set(fields).issubset(permitted):
                    raise HTTPException(422, "INVALID_ARGUMENT")
                if not object_allowed(principal, args):
                    raise HTTPException(404, "TOOL_NOT_AVAILABLE")
            with self.telemetry.span("execution.quota"):
                await self.quota(principal, tool_id)
            await self.schema(principal, tool_id, selected["version"])
            if definition["risk"] == "approval":
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
            if tool_id == "communication.notify_leave":
                await self.downstream("hr.leave_status", args)
            if tool_id in {"communication.send_notification", "support.resolve_ticket"}:
                await self.confirm_refund(tool_id, args)
            async with self.db.begin() as conn:
                dispatch = await rows(
                    conn,
                    """UPDATE executions SET status='RUNNING', tool_id=:tool,
                       version=:version, dispatch_risk=:risk
                       WHERE id=:id AND status='RECEIVED' RETURNING id""",
                    id=trace,
                    tool=tool_id,
                    version=selected["version"],
                    risk="write" if write else "read",
                )
            if not dispatch:
                raise HTTPException(409, "EXECUTION_EXPIRED")
            status = "RUNNING"
            with self.telemetry.span("execution.dispatch"):
                if write:
                    payload = await self.downstream(tool_id, args)
                else:
                    payload = await self.read_dispatch.call(
                        tool_id.split(".", 1)[0], lambda: self.downstream(tool_id, args)
                    )
            with self.telemetry.span("execution.validate_result"):
                if output_model is None:
                    if not Draft202012Validator(definition["output_schema"]).is_valid(
                        payload
                    ):
                        raise HTTPException(502, "UPSTREAM_SCHEMA_MISMATCH")
                    result = payload
                else:
                    result = output_model.model_validate_json(
                        json.dumps(payload)
                    ).model_dump(mode="json")
                result = redact(result)
                if fields:
                    result = {field: result[field] for field in fields}
            status = "SUCCEEDED"
        except asyncio.CancelledError:
            if status == "DENIED":
                status = "RECEIVED"
            raise
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
                status = (
                    "UNKNOWN"
                    if write
                    and code
                    not in {
                        "RECORD_NOT_AVAILABLE",
                        "INVALID_ARGUMENT",
                        "LEAVE_CONFLICT",
                        "INVALID_LEAVE_DATES",
                        "INVALID_REPORTING_WINDOW",
                        "CHANNEL_EXISTS",
                        "INVALID_TICKET_TRANSITION",
                    }
                    else "FAILED"
                )
            if status not in {"RECEIVED", "RUNNING", "QUEUED"}:
                if code == "TOOL_NOT_AVAILABLE":
                    selected = None
                async with self.db.begin() as conn:
                    updated = await rows(
                        conn,
                        """
                        UPDATE executions SET tool_id=:tool, version=:version, status=:status,
                            error_code=:code, finished_at=now()
                        WHERE id=:id AND status IN ('RECEIVED', 'RUNNING') RETURNING id
                    """,
                        id=trace,
                        tool=tool_id if selected else None,
                        version=selected["version"] if selected else None,
                        status=status,
                        code=code,
                    )
                    if not updated:
                        stored = await rows(
                            conn,
                            "SELECT status, error_code FROM executions WHERE id=:id",
                            id=trace,
                        )
                        status = stored[0]["status"]
                        code = stored[0]["error_code"]
                        result = None
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
            self.telemetry.executions.labels(status=status).observe(
                time.monotonic() - started
            )
        return {
            "id": str(trace),
            "status": status,
            "error_code": code,
            "retry_after_seconds": 60 if code == "RATE_LIMITED" else None,
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

    def require_protected(self) -> ProtectedDispatch:
        if self.protected is None:
            raise HTTPException(503, "DURABLE_ADMISSION_DISABLED")
        return self.protected

    async def is_durable_execution(self, execution_id: UUID) -> bool:
        try:
            async with self.db.connect() as conn:
                [row] = await rows(
                    conn, "SELECT is_durable_approval(:id) AS durable", id=execution_id
                )
        except SQLAlchemyError:
            raise HTTPException(503, "EXECUTION_COORDINATION_UNAVAILABLE") from None
        return bool(row["durable"])

    async def audit_events(self, principal: Principal):
        async with self.db.connect() as conn:
            legacy = await rows(
                conn,
                """
                SELECT id,actor,action,resource_id,created_at FROM audit_events
                WHERE NOT public.is_durable_approval(resource_id)
                  AND NOT starts_with(action,'DURABLE_')
                ORDER BY created_at DESC,id DESC LIMIT 100
            """,
            )
        if self.protected is None:
            return legacy
        durable = await self.protected.audit_events(principal)
        return sorted(
            [*legacy, *durable],
            key=lambda item: (item["created_at"], item["id"]),
            reverse=True,
        )[:100]

    async def list_approvals(self, principal: Principal):
        legacy = await self.approvals.list(principal)
        if self.protected is None or await dispatch_mode(self.db) == "LEGACY":
            return legacy
        protected = await self.protected.list_approvals(principal)
        return sorted(
            [*legacy, *protected], key=lambda item: item["requested_at"], reverse=True
        )[:100]

    async def decide_approval(
        self, principal, execution_id, approve, reason, respond_async=False
    ):
        mode = await dispatch_mode(self.db)
        if mode == "PAUSED":
            raise HTTPException(503, "DURABLE_ADMISSION_DISABLED")
        if mode == "DURABLE":
            protected = self.require_protected()
            return await protected.decide(
                principal, execution_id, approve, reason, respond_async
            )
        if await self.is_durable_execution(execution_id):
            raise HTTPException(404, "APPROVAL_NOT_AVAILABLE")
        if approve and principal.can_approve:
            await self.quota(principal, "approve_refund")
        return await self.approvals.decide(principal, execution_id, approve, reason)

    async def execution_result(self, principal, execution_id, fields):
        if await self.is_durable_execution(execution_id):
            if self.protected is None:
                raise HTTPException(404, "EXECUTION_NOT_AVAILABLE")
            return await self.protected.result(principal, execution_id, fields)
        values = await self.executions(principal, execution_id)
        if not values:
            raise HTTPException(404, "EXECUTION_NOT_AVAILABLE")
        [item] = values
        return {
            "id": str(execution_id),
            "status": item["status"],
            "error_code": item["error_code"],
            "result": None,
            "result_availability": "LEGACY_UNAVAILABLE",
        }

    async def execution_events(self, principal: Principal, trace: UUID):
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT ev.id, ev.status, ev.error_code, ev.recorded_at
                FROM execution_events ev JOIN executions e ON e.id=ev.execution_id
                WHERE e.id=:trace AND (e.principal=:principal OR :admin)
                ORDER BY ev.id
                """,
                trace=trace,
                principal=principal.id,
                admin=principal.can_publish or principal.can_approve,
            )

    async def executions(
        self,
        principal: Principal,
        trace: UUID | None = None,
        hours: int | None = None,
        status: str | None = None,
    ):
        async with self.db.connect() as conn:
            legacy = await rows(
                conn,
                """
                SELECT id, principal, tool_id, version, status, error_code, started_at, finished_at,
                       request_id, otel_trace_id
                FROM executions
                WHERE NOT is_durable_approval(id) AND (principal=:principal OR :admin)
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

        if self.protected is None:
            return legacy
        durable = await self.protected.execution_metadata(
            principal, trace, hours, status
        )
        return sorted(
            [*legacy, *durable],
            key=lambda item: (-item["started_at"].timestamp(), item["id"]),
        )[:100]


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
