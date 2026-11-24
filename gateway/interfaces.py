from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Response,
)
from fastapi import (
    Query as QueryParam,
)
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel, Field, StringConstraints, model_serializer

from control_plane.catalog import Catalog
from control_plane.crawler import Crawler
from gateway.discovery import Strategy
from gateway.identity import Identity, Principal
from gateway.runtime import Runtime
from gateway.workflow import DuplicateWorkflow
from shared.contracts import Identifier
from shared.database import rows

Query = Annotated[str, Field(max_length=128)]
Key = Annotated[str, Field(min_length=1, max_length=128)]
Limit = Annotated[int, Field(ge=1, le=50)]
Version = Annotated[int, Field(ge=1, le=2147483647)]
Projection = Annotated[list[Key], Field(min_length=1, max_length=20)]


class Credentials(BaseModel):
    username: Key
    password: Annotated[str, Field(min_length=1, max_length=256)]


class Search(BaseModel):
    query: Query = ""
    service_id: Key | None = None
    limit: Limit = 20
    strategy: Strategy = "keyword"


class Invocation(BaseModel):
    tool_id: Key
    arguments: dict
    version: Version | None = None
    fields: Projection | None = None
    idempotency_key: Key | None = None


class Publication(BaseModel):
    version: Version
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)
    ]


class ServiceEdit(BaseModel):
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]


class Registration(ServiceEdit):
    target: Key


class Decision(BaseModel):
    reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)
    ]


class StartWorkflow(BaseModel):
    id: UUID
    order_id: Identifier


class RequestRefund(BaseModel):
    transaction_id: Identifier


class SessionToken(BaseModel):
    access_token: str
    expires_in: int


class CallerIdentity(BaseModel):
    id: str
    kind: str
    profile: str


class ServiceRecord(BaseModel):
    id: str
    name: str
    protocol: str
    owner: str
    published_tools: int
    health: str
    checked_at: datetime | None
    health_error: str | None


class ToolSummary(BaseModel):
    id: str
    service_id: str
    version: int
    description: str
    risk: str
    rank: int


class ToolSchema(BaseModel):
    id: str
    service_id: str
    version: int
    contract: dict


class RegistryEntry(ToolSchema):
    active_version: int | None
    lifecycle: str


class PublicationResult(BaseModel):
    id: str
    version: int
    status: str


class InvocationResult(BaseModel):
    id: UUID
    status: str
    error_code: str | None
    result: dict | None
    approval_id: UUID | None = None
    retry_after_seconds: int | None = None

    phase: str | None = None
    approval_status: str | None = None
    result_availability: str | None = None
    next_check_at: datetime | None = None

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        result = handler(self)
        for key in ("phase", "approval_status", "result_availability", "next_check_at"):
            if result.get(key) is None:
                result.pop(key, None)
        return result


def prefers_async(prefer: str) -> bool:
    return any(value.strip().lower() == "respond-async" for value in prefer.split(","))


def invocation_headers(response: Response, result: dict, asynchronous: bool) -> None:
    if result.get("retry_after_seconds"):
        response.headers["Retry-After"] = str(result["retry_after_seconds"])
    if asynchronous and result.get("phase") and result["status"] == "RUNNING":
        response.status_code = 202
        response.headers["Preference-Applied"] = "respond-async"
        response.headers["Location"] = f"/api/v1/executions/{result['id']}"


class ExecutionRecord(BaseModel):
    id: UUID
    principal: str
    tool_id: str | None
    version: int | None
    status: str
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None
    request_id: str | None
    otel_trace_id: str | None
    phase: str | None = None
    approval_status: str | None = None

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        result = handler(self)
        for key in ("phase", "approval_status"):
            if result.get(key) is None:
                result.pop(key, None)
        return result


class ExecutionEvent(BaseModel):
    id: int
    status: str
    error_code: str | None
    recorded_at: datetime


def public_tools(values: list[dict]) -> list[dict]:
    return [
        {
            "id": row["id"],
            "service_id": row["service_id"],
            "version": row["version"],
            "description": row["contract"]["description"],
            "risk": row["contract"]["risk"],
            "rank": rank,
        }
        for rank, row in enumerate(values, 1)
    ]


def build_api(
    identity: Identity, runtime: Runtime, catalog: Catalog, crawler: Crawler
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    workflow = DuplicateWorkflow(runtime)

    async def caller(authorization: Annotated[str, Header()] = "") -> Principal:
        with runtime.telemetry.span("auth.resolve_principal"):
            return await identity.principal(authorization)

    @router.post("/auth/login", response_model=SessionToken)
    async def login(body: Credentials):
        return {
            "access_token": await identity.login(body.username, body.password),
            "expires_in": 3600,
        }

    @router.get("/auth/me", response_model=CallerIdentity)
    async def me(principal: Principal = Depends(caller)):
        return {
            "id": principal.id,
            "kind": principal.kind,
            "profile": principal.profile,
        }

    @router.post("/auth/logout", status_code=204)
    async def logout(authorization: Annotated[str, Header()] = ""):
        await identity.revoke(authorization)

    @router.get("/services", response_model=list[ServiceRecord])
    async def services(principal: Principal = Depends(caller)):
        return await catalog.services(principal)

    @router.get("/tools", response_model=list[RegistryEntry | ToolSummary])
    async def tools(principal: Principal = Depends(caller)):
        if principal.can_publish:
            return await catalog.registry(principal)
        return public_tools(await runtime.tools(principal, limit=50))

    @router.post("/tools/{tool_id}/publish", response_model=PublicationResult)
    async def publish(
        tool_id: Key, body: Publication, principal: Principal = Depends(caller)
    ):
        return await catalog.publish(principal, tool_id, body.version, body.reason)

    @router.post("/discovery/search", response_model=list[ToolSummary])
    async def search(body: Search, principal: Principal = Depends(caller)):
        return public_tools(
            await runtime.tools(
                principal, body.service_id, body.query, body.limit, body.strategy
            )
        )

    @router.get("/discovery/tools/{tool_id}/schema", response_model=ToolSchema)
    async def schema(
        tool_id: Key,
        version: Version | None = None,
        principal: Principal = Depends(caller),
    ):
        return await runtime.schema(principal, tool_id, version)

    @router.post("/executions", response_model=InvocationResult)
    async def invoke(
        body: Invocation,
        response: Response,
        principal: Principal = Depends(caller),
        prefer: Annotated[str, Header()] = "",
    ):
        asynchronous = prefers_async(prefer)
        result = await runtime.invoke(
            principal,
            body.tool_id,
            body.arguments,
            body.version,
            body.fields,
            body.idempotency_key,
            respond_async=asynchronous,
        )
        invocation_headers(response, result, asynchronous)
        return result

    @router.get("/executions", response_model=list[ExecutionRecord])
    async def executions(
        hours: Annotated[int, Field(ge=1, le=168)] | None = None,
        status: Key | None = None,
        principal: Principal = Depends(caller),
    ):
        return await runtime.executions(principal, hours=hours, status=status)

    @router.get("/executions/{trace_id}", response_model=ExecutionRecord)
    async def execution(trace_id: UUID, principal: Principal = Depends(caller)):
        values = await runtime.executions(principal, trace_id)
        if not values:
            raise HTTPException(404, "EXECUTION_NOT_AVAILABLE")
        return values[0]

    @router.get("/executions/{trace_id}/result", response_model=InvocationResult)
    async def execution_result(
        trace_id: UUID,
        principal: Principal = Depends(caller),
        fields: Annotated[Projection | None, QueryParam()] = None,
    ):
        return await runtime.execution_result(principal, trace_id, fields)

    @router.get("/executions/{trace_id}/events", response_model=list[ExecutionEvent])
    async def execution_events(trace_id: UUID, principal: Principal = Depends(caller)):
        await execution(trace_id, principal)
        return await runtime.execution_events(principal, trace_id)

    @router.get("/approvals")
    async def approvals(principal: Principal = Depends(caller)):
        return await runtime.list_approvals(principal)

    @router.post("/approvals/{approval_id}/{decision}", response_model=InvocationResult)
    async def decide(
        approval_id: UUID,
        decision: Literal["approve", "reject"],
        body: Decision,
        response: Response,
        principal: Principal = Depends(caller),
        prefer: Annotated[str, Header()] = "",
    ):
        asynchronous = prefers_async(prefer)
        result = await runtime.decide_approval(
            principal,
            approval_id,
            decision == "approve",
            body.reason,
            asynchronous,
        )
        invocation_headers(response, result, asynchronous)
        return result

    @router.get("/audit-events")
    async def audit(principal: Principal = Depends(caller)):
        if not principal.can_publish and not principal.can_approve:
            raise HTTPException(403, "FORBIDDEN")
        return await runtime.audit_events(principal)

    @router.get("/overview")
    async def overview(
        hours: Annotated[int, Field(ge=1, le=168)] = 24,
        principal: Principal = Depends(caller),
    ):
        async with runtime.db.connect() as conn:
            outcomes = await rows(
                conn,
                """
                SELECT status,count(*) AS count FROM executions
                WHERE (principal=:principal OR :reviewer) AND started_at >= now() - make_interval(hours=>:hours)
                GROUP BY status ORDER BY status
            """,
                principal=principal.id,
                reviewer=principal.can_publish or principal.can_approve,
                hours=hours,
            )
        async with runtime.db.connect() as conn:
            pending = await rows(
                conn,
                """
                SELECT count(*) AS count FROM approvals
                WHERE customer_id=:customer AND (requester=:principal OR :reviewer)
                  AND status='PENDING' AND expires_at>now()
            """,
                customer=principal.customer,
                principal=principal.id,
                reviewer=principal.can_publish or principal.can_approve,
            )
        return {
            "hours": hours,
            "outcomes": outcomes,
            "services": await catalog.services(principal),
            "pending": pending[0]["count"],
        }

    @router.get("/workflows")
    async def workflows(principal: Principal = Depends(caller)):
        return await workflow.list(principal)

    @router.post("/workflows")
    async def start(body: StartWorkflow, principal: Principal = Depends(caller)):
        return await workflow.start(principal, body.id, body.order_id)

    @router.post("/workflows/{run_id}/refund")
    async def request_refund(
        run_id: UUID, body: RequestRefund, principal: Principal = Depends(caller)
    ):
        return await workflow.request(principal, run_id, body.transaction_id)

    @router.post("/workflows/{run_id}/complete")
    async def complete(run_id: UUID, principal: Principal = Depends(caller)):
        return await workflow.complete(principal, run_id)

    @router.get("/service-targets")
    async def targets(principal: Principal = Depends(caller)):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        return [
            {"id": key, "protocol": target.protocol, "endpoint": target.location(key)}
            for key, target in crawler.targets.items()
        ]

    @router.post("/services", status_code=201)
    async def register(body: Registration, principal: Principal = Depends(caller)):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        return await crawler.register(principal.id, body.target, body.name)

    @router.patch("/services/{service_id}")
    async def edit_service(
        service_id: Key, body: ServiceEdit, principal: Principal = Depends(caller)
    ):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        return await crawler.rename(service_id, body.name)

    @router.post("/services/{service_id}/test-connection")
    async def test_connection(service_id: Key, principal: Principal = Depends(caller)):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        return await crawler.test_connection(service_id)

    @router.get("/crawl-jobs")
    async def crawl_jobs(principal: Principal = Depends(caller)):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        return await crawler.jobs()

    @router.get("/crawl-jobs/{job_id}")
    async def crawl_job(job_id: UUID, principal: Principal = Depends(caller)):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        values = await crawler.jobs(job_id)
        if not values:
            raise HTTPException(404, "CRAWL_NOT_AVAILABLE")
        return values[0]

    @router.post("/services/{service_id}/crawl", status_code=202)
    async def start_crawl(service_id: Key, principal: Principal = Depends(caller)):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        return await crawler.enqueue(service_id, principal.id)

    @router.get("/tools/{tool_id}/versions")
    async def tool_versions(tool_id: Key, principal: Principal = Depends(caller)):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        return await catalog.versions(tool_id)

    @router.post("/tools/{tool_id}/{action}")
    async def release(
        tool_id: Key,
        action: Literal["review", "disable", "archive", "rollback"],
        body: Publication,
        principal: Principal = Depends(caller),
    ):
        return await catalog.release(
            principal, tool_id, action, body.reason, body.version
        )

    return router


def build_mcp(
    identity: Identity,
    runtime: Runtime,
    hosts: list[str],
    *,
    protected_results_enabled: bool = False,
):
    server = FastMCP("ToolMesh AI", auth=identity, mask_error_details=True)

    async def caller() -> Principal:
        token = get_access_token()
        if token is None:
            raise ToolError("AUTH_REQUIRED")
        try:
            with runtime.telemetry.span("auth.resolve_principal"):
                return await identity.principal(f"Bearer {token.token}")
        except HTTPException as exc:
            raise ToolError(str(exc.detail)) from None

    @server.tool
    async def discover_server(query: Query, limit: Limit = 20) -> dict:
        """Find published services available to the caller."""
        values = await runtime.servers(await caller(), query, limit)
        return {
            "servers": [{**row, "rank": rank} for rank, row in enumerate(values, 1)]
        }

    @server.tool
    async def discover_tools(
        server_id: Key,
        query: Query = "",
        limit: Limit = 20,
        strategy: Strategy = "keyword",
    ) -> dict:
        """List permitted published tools without expanding their schemas."""
        return {
            "tools": public_tools(
                await runtime.tools(await caller(), server_id, query, limit, strategy)
            )
        }

    @server.tool
    async def get_tool_schema(tool_id: Key, version: Version | None = None) -> dict:
        """Fetch the currently published immutable contract for an authorized tool."""
        try:
            return await runtime.schema(await caller(), tool_id, version)
        except HTTPException as exc:
            raise ToolError(str(exc.detail)) from None

    @server.tool
    async def invoke_tool(
        tool_id: Key,
        arguments: dict,
        version: Version | None = None,
        fields: Projection | None = None,
        idempotency_key: Key | None = None,
        respond_async: bool = False,
    ) -> dict:
        """Invoke a published operation and return its persisted execution ID."""
        try:
            return await runtime.invoke(
                await caller(),
                tool_id,
                arguments,
                version,
                fields,
                idempotency_key,
                respond_async=respond_async,
            )
        except HTTPException as exc:
            raise ToolError(str(exc.detail)) from None

    async def get_execution_result(
        execution_id: UUID, fields: Projection | None = None
    ) -> dict:
        """Retrieve authorized execution results without dispatching."""
        try:
            return await runtime.execution_result(await caller(), execution_id, fields)
        except HTTPException as exc:
            raise ToolError(str(exc.detail)) from None

    if protected_results_enabled:
        server.tool(get_execution_result)

    return server.http_app(
        path="/",
        stateless_http=True,
        json_response=True,
        allowed_hosts=hosts,
        allowed_origins=[],
    )
