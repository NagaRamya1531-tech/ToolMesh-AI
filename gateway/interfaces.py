from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from pydantic import BaseModel, Field, StringConstraints

from control_plane.catalog import Catalog
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


class ServiceRecord(BaseModel):
    id: str
    name: str
    protocol: str
    owner: str
    published_tools: int


class ToolSummary(BaseModel):
    id: str
    service_id: str
    version: int
    description: str
    risk: str


class ToolSchema(BaseModel):
    id: str
    service_id: str
    version: int
    contract: dict


class RegistryEntry(ToolSchema):
    active_version: int | None


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


class ExecutionRecord(BaseModel):
    id: UUID
    principal: str
    tool_id: str | None
    version: int | None
    status: str
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None


def public_tools(values: list[dict]) -> list[dict]:
    return [
        {
            "id": row["id"],
            "service_id": row["service_id"],
            "version": row["version"],
            "description": row["contract"]["description"],
            "risk": row["contract"]["risk"],
        }
        for row in values
    ]


def build_api(identity: Identity, runtime: Runtime, catalog: Catalog) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    workflow = DuplicateWorkflow(runtime)

    async def caller(authorization: Annotated[str, Header()] = "") -> Principal:
        return await identity.principal(authorization)

    @router.post("/auth/login", response_model=SessionToken)
    async def login(body: Credentials):
        return {
            "access_token": await identity.login(body.username, body.password),
            "expires_in": 3600,
        }

    @router.get("/auth/me", response_model=CallerIdentity)
    async def me(principal: Principal = Depends(caller)):
        return {"id": principal.id, "kind": principal.kind}

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
            await runtime.tools(principal, body.service_id, body.query, body.limit)
        )

    @router.get("/discovery/tools/{tool_id}/schema", response_model=ToolSchema)
    async def schema(
        tool_id: Key,
        version: Version | None = None,
        principal: Principal = Depends(caller),
    ):
        return await runtime.schema(principal, tool_id, version)

    @router.post("/executions", response_model=InvocationResult)
    async def invoke(body: Invocation, principal: Principal = Depends(caller)):
        return await runtime.invoke(
            principal,
            body.tool_id,
            body.arguments,
            body.version,
            body.fields,
            body.idempotency_key,
        )

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

    @router.get("/approvals")
    async def approvals(principal: Principal = Depends(caller)):
        return await runtime.approvals.list(principal)

    @router.post("/approvals/{approval_id}/{decision}", response_model=InvocationResult)
    async def decide(
        approval_id: UUID,
        decision: Literal["approve", "reject"],
        body: Decision,
        principal: Principal = Depends(caller),
    ):
        if decision == "approve" and principal.can_approve:
            await runtime.quota(principal, "approve_refund")
        return await runtime.approvals.decide(
            principal, approval_id, decision == "approve", body.reason
        )

    @router.get("/audit-events")
    async def audit(principal: Principal = Depends(caller)):
        if not principal.can_publish and not principal.can_approve:
            raise HTTPException(403, "FORBIDDEN")
        async with runtime.db.connect() as conn:
            return await rows(
                conn,
                "SELECT id,actor,action,resource_id,created_at FROM audit_events ORDER BY created_at DESC,id DESC LIMIT 100",
            )

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

    return router


def build_mcp(identity: Identity, runtime: Runtime, hosts: list[str]):
    server = FastMCP("ToolMesh AI", auth=identity, mask_error_details=True)

    async def caller() -> Principal:
        token = get_access_token()
        if token is None:
            raise ToolError("AUTH_REQUIRED")
        try:
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
        server_id: Key, query: Query = "", limit: Limit = 20
    ) -> dict:
        """List permitted published tools without expanding their schemas."""
        return {
            "tools": public_tools(
                await runtime.tools(await caller(), server_id, query, limit)
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
    ) -> dict:
        """Invoke a published operation and return its persisted execution ID."""
        return await runtime.invoke(
            await caller(), tool_id, arguments, version, fields, idempotency_key
        )

    return server.http_app(
        path="/",
        stateless_http=True,
        json_response=True,
        allowed_hosts=hosts,
        allowed_origins=[],
    )
