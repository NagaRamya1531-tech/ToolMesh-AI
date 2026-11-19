"""Administrator endpoints for sandbox identities, scoped policy and quota settings."""

from secrets import token_urlsafe
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.exc import IntegrityError

from gateway.access import object_allowed
from gateway.approvals import event
from gateway.identity import Identity, digest, resolve_principal
from gateway.runtime import Runtime
from shared.authorization_revision import guard_authorization_write
from shared.database import execute, rows

Key = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Reason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)
]


class AgentCreate(BaseModel):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{2,47}$")]
    profile: Literal["support", "finance", "it", "hr"]


class AgentUpdate(BaseModel):
    enabled: bool


class PolicyCreate(BaseModel):
    principal: Key
    tool_id: Key
    effect: Literal["allow", "deny"]
    reason: Reason


class PolicyUpdate(BaseModel):
    enabled: bool
    version: Annotated[int, Field(ge=1, lt=2147483647)]
    reason: Reason


class Simulation(BaseModel):
    principal: Key
    tool_id: Key
    arguments: dict


class Quotas(BaseModel):
    per_tool: Annotated[int, Field(ge=1, le=600)]
    global_limit: Annotated[int, Field(ge=1, le=10000)]


def build_governance(identity: Identity, runtime: Runtime):
    router = APIRouter(prefix="/api/v1")
    db = runtime.db

    async def admin(authorization: Annotated[str, Header()] = ""):
        principal = await identity.principal(authorization)
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        return principal

    @router.get("/agents")
    async def agents(_=Depends(admin)):
        async with db.connect() as conn:
            return await rows(
                conn,
                "SELECT id,profile,enabled,origin FROM agent_accounts ORDER BY profile,id LIMIT 100",
            )

    @router.post("/agents", status_code=201)
    async def create_agent(body: AgentCreate, _=Depends(admin)):
        if body.id in {"platform-admin", "refund-approver"}:
            raise HTTPException(409, "AGENT_ID_CONFLICT")
        credential = token_urlsafe(32)
        try:
            async with db.begin() as conn:
                await guard_authorization_write(conn)
                await execute(
                    conn,
                    "INSERT INTO agent_accounts(id,profile,credential_hash) VALUES (:id,:profile,:hash)",
                    id=body.id,
                    profile=body.profile,
                    hash=digest(credential),
                )
        except IntegrityError:
            raise HTTPException(409, "AGENT_ID_CONFLICT") from None
        return {"id": body.id, "credential": credential}

    @router.patch("/agents/{agent_id}")
    async def update_agent(agent_id: Key, body: AgentUpdate, _=Depends(admin)):
        async with db.begin() as conn:
            await guard_authorization_write(conn)
            result = await rows(
                conn,
                "UPDATE agent_accounts SET enabled=:enabled WHERE id=:id RETURNING id,profile,enabled,origin",
                id=agent_id,
                enabled=body.enabled,
            )
            if not result:
                raise HTTPException(404, "AGENT_NOT_AVAILABLE")
            if not body.enabled:
                await execute(
                    conn, "DELETE FROM sessions WHERE principal=:id", id=agent_id
                )
        return result[0]

    @router.get("/policies")
    async def policies(_=Depends(admin)):
        async with db.connect() as conn:
            return await rows(
                conn,
                "SELECT * FROM policy_rules ORDER BY principal,tool_id,effect LIMIT 150",
            )

    @router.get("/policies/{policy_id}/history")
    async def history(policy_id: UUID, _=Depends(admin)):
        async with db.connect() as conn:
            return await rows(
                conn,
                "SELECT version,actor,reason,enabled,changed_at FROM policy_revisions WHERE policy_id=:id ORDER BY version DESC LIMIT 50",
                id=policy_id,
            )

    @router.post("/policies", status_code=201)
    async def create_policy(body: PolicyCreate, actor=Depends(admin)):
        ident = uuid4()
        try:
            async with db.begin() as conn:
                await guard_authorization_write(conn)
                agent = await resolve_principal(conn, body.principal)
                tool = await rows(
                    conn, "SELECT service_id FROM tools WHERE id=:id", id=body.tool_id
                )
                if (
                    not agent
                    or agent.kind != "agent"
                    or not tool
                    or tool[0]["service_id"] not in agent.services
                ):
                    raise HTTPException(422, "INVALID_POLICY_SCOPE")
                await execute(
                    conn,
                    """
                    INSERT INTO policy_rules VALUES(:id,:principal,:tool,:customer,:effect,true,1)
                """,
                    id=ident,
                    principal=agent.id,
                    tool=body.tool_id,
                    customer=agent.customer,
                    effect=body.effect,
                )
                await execute(
                    conn,
                    "INSERT INTO policy_revisions(policy_id,version,actor,reason,enabled) VALUES(:id,1,:actor,:reason,true)",
                    id=ident,
                    actor=actor.id,
                    reason=body.reason,
                )
                await event(conn, actor.id, "POLICY_CREATED", ident)
        except IntegrityError:
            raise HTTPException(409, "POLICY_EXISTS") from None
        return {"id": ident, "version": 1}

    @router.patch("/policies/{policy_id}")
    async def update_policy(policy_id: UUID, body: PolicyUpdate, actor=Depends(admin)):
        async with db.begin() as conn:
            await guard_authorization_write(conn)
            result = await rows(
                conn,
                "UPDATE policy_rules SET enabled=:enabled,version=version+1 WHERE id=:id AND version=:version RETURNING version",
                id=policy_id,
                version=body.version,
                enabled=body.enabled,
            )
            if not result:
                raise HTTPException(409, "POLICY_VERSION_CONFLICT")
            await execute(
                conn,
                "INSERT INTO policy_revisions(policy_id,version,actor,reason,enabled) VALUES(:id,:version,:actor,:reason,:enabled)",
                id=policy_id,
                version=result[0]["version"],
                actor=actor.id,
                reason=body.reason,
                enabled=body.enabled,
            )
            await event(conn, actor.id, "POLICY_UPDATED", policy_id)
        return result[0]

    @router.post("/policies/simulate")
    async def simulate(body: Simulation, _=Depends(admin)):
        async with db.connect() as conn:
            agent = await resolve_principal(conn, body.principal)
            if not agent:
                return {"allowed": False, "reason": "PRINCIPAL_UNAVAILABLE"}
            if not object_allowed(agent, body.arguments):
                return {"allowed": False, "reason": "OBJECT_SCOPE_DENIED"}
            active = await rows(
                conn,
                """
                SELECT effect FROM policy_rules
                WHERE principal=:id AND tool_id=:tool AND customer_id=:customer AND enabled AND effect='deny'
            """,
                id=agent.id,
                tool=body.tool_id,
                customer=agent.customer,
            )
            if active:
                return {"allowed": False, "reason": "EXPLICIT_DENY"}
        try:
            tool = await runtime.schema(agent, body.tool_id, None)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return {"allowed": False, "reason": "TOOL_NOT_AVAILABLE"}
        return {
            "allowed": True,
            "reason": "APPROVAL_REQUIRED"
            if tool["contract"]["risk"] == "approval"
            else "POLICY_ALLOWED",
        }

    @router.get("/settings/quotas")
    async def quotas(_=Depends(admin)):
        async with db.connect() as conn:
            return (
                await rows(
                    conn, "SELECT per_tool,global_limit FROM quota_settings WHERE id"
                )
            )[0]

    @router.patch("/settings/quotas")
    async def set_quotas(body: Quotas, actor=Depends(admin)):
        async with db.begin() as conn:
            await execute(
                conn,
                "UPDATE quota_settings SET per_tool=:per_tool,global_limit=:global_limit WHERE id",
                **body.model_dump(),
            )
            await event(conn, actor.id, "QUOTAS_UPDATED", UUID(int=0))
        return body

    return router
