"""Public boundary for the two protected commands. This module never sends."""

import asyncio
import json
from datetime import timedelta
from hashlib import sha256
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.access import POLICY, scope_params
from gateway.execution_approval import ExecutionApprovals
from gateway.execution_contracts import (
    ProtectedIntentScope,
    ProtectedTool,
    SealedPayload,
)
from gateway.execution_crypto import (
    CommandDigests,
    PayloadContext,
    PayloadProtection,
    PayloadUnavailable,
    canonical_json,
)
from gateway.execution_store import CommandStore, CommandStoreError, CommandSubmission
from gateway.identity import Principal, resolve_principal
from gateway.redaction import redact
from shared.database import rows

PROTECTED_TOOLS = {"payments.issue_refund", "it.restart_service"}
WORKSPACE = "default"


# The same current record boundary governs execution metadata and durable audit.
RECORD_VISIBLE = (
    "c.workspace_id=:workspace AND c.customer_id=:customer "
    "AND ((c.requester=:principal AND (" + POLICY + ")) "
    "OR (:human AND EXISTS ("
    "SELECT 1 FROM execution_approval_entitlements q "
    "WHERE q.principal=:principal AND q.customer_id=c.customer_id "
    "AND q.tool_id=c.tool_id AND q.enabled)))"
)


def public_error(exc: CommandStoreError) -> HTTPException:
    code = str(exc)
    if code in {"TOOL_NOT_AVAILABLE", "APPROVAL_NOT_AVAILABLE"}:
        return HTTPException(404, code)
    if code in {"INVALID_ARGUMENT", "INVALID_DECISION", "IDEMPOTENCY_KEY_REQUIRED"}:
        return HTTPException(422, code)
    if code in {
        "IDEMPOTENCY_CONFLICT",
        "VERSION_INCOMPATIBLE",
        "APPROVAL_ALREADY_DECIDED",
        "REQUESTER_AUTHORIZATION_CHANGED",
        "LEGACY_RESERVATION_UNAVAILABLE",
    }:
        return HTTPException(409, code)
    return HTTPException(503, code)


async def dispatch_mode(db: AsyncEngine) -> str:
    try:
        async with db.connect() as conn:
            result = await rows(
                conn, "SELECT mode FROM execution_dispatch_gate WHERE singleton"
            )
    except SQLAlchemyError:
        raise HTTPException(503, "EXECUTION_COORDINATION_UNAVAILABLE") from None
    if len(result) != 1 or result[0]["mode"] not in {"LEGACY", "DURABLE", "PAUSED"}:
        raise HTTPException(503, "EXECUTION_COORDINATION_UNAVAILABLE")
    return str(result[0]["mode"])


def projection(execution_id, status, phase, approval, error=None, **extra):
    return {
        "id": str(execution_id),
        "status": status,
        "phase": phase,
        "approval_status": approval,
        "approval_id": str(execution_id),
        "error_code": "UNKNOWN_OUTCOME" if status == "UNKNOWN" else error,
        "result": None,
        **({"retry_after_seconds": 1} if status == "RUNNING" else {}),
        **extra,
    }


class ProtectedDispatch:
    def __init__(
        self,
        db: AsyncEngine,
        payloads: PayloadProtection,
        digests: CommandDigests,
        wait_seconds: float,
        dispatch_window: timedelta,
    ):
        self.db = db
        self.payloads = payloads
        self.commands = CommandStore(db, payloads, digests)
        self.approvals = ExecutionApprovals(db, payloads, digests, dispatch_window)
        self.wait_seconds = wait_seconds

    async def valid_database_identity(self) -> bool:
        """Verify the configured login's effective privileges before enabling routes."""
        async with self.db.connect() as conn:
            identities = await rows(
                conn,
                """
                SELECT current_database()=:database AND current_user=:login
                  AND session_user=current_user AND me.rolcanlogin
                  AND NOT (me.rolsuper OR me.rolcreaterole OR me.rolcreatedb
                           OR me.rolreplication OR me.rolbypassrls)
                  AND pg_has_role(current_user, protected.oid, 'USAGE')
                  AND NOT (protected.rolcanlogin OR protected.rolsuper OR protected.rolcreaterole
                           OR protected.rolcreatedb OR protected.rolreplication OR protected.rolbypassrls)
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_roles r
                    WHERE r.rolname NOT IN (current_user,'toolmesh_protected_api')
                      AND pg_has_role(current_user,r.oid,'MEMBER')
                  )
                  AND NOT has_database_privilege(current_database(),'CREATE')
                  AND NOT has_schema_privilege('public','CREATE')
                  AND has_column_privilege('public.execution_commands','state','UPDATE')
                  AND has_column_privilege('public.execution_receipts','ciphertext','SELECT')
                  AND NOT has_column_privilege('public.execution_dispatch_gate','mode','UPDATE')
                  AND NOT has_any_column_privilege('public.execution_dispatch_gate','INSERT')
                  AND NOT has_table_privilege('public.execution_dispatch_gate','DELETE,TRUNCATE,TRIGGER')
                  AND NOT EXISTS (
                    SELECT 1 FROM unnest(ARRAY['public.execution_attempts','public.execution_receipts']) AS t(name)
                    WHERE has_any_column_privilege(t.name,'INSERT,UPDATE')
                       OR has_table_privilege(t.name,'DELETE,TRUNCATE,TRIGGER')
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE p.prosecdef AND n.nspname <> 'pg_catalog'
                      AND p.prorettype <> 'pg_catalog.trigger'::regtype
                      AND has_function_privilege(current_user,p.oid,'EXECUTE')
                  )
                  AS valid
                FROM pg_roles me JOIN pg_roles protected ON protected.rolname='toolmesh_protected_api'
                WHERE me.rolname=current_user
            """,
                database=self.db.url.database,
                login=self.db.url.username,
            )
        return len(identities) == 1 and bool(identities[0]["valid"])

    async def submit(
        self, principal, tool, arguments, version, fields, key, respond_async
    ):
        try:
            stored = await self.commands.submit(
                CommandSubmission(
                    WORKSPACE,
                    principal.id,
                    cast(ProtectedTool, tool),
                    arguments,
                    key or "",
                    version,
                    tuple(fields or ()),
                )
            )
        except CommandStoreError as exc:
            raise public_error(exc) from None
        if stored.status == "PENDING_APPROVAL":
            return projection(
                stored.execution_id,
                stored.status,
                stored.status,
                stored.approval_status.value,
            )
        current = await self.result(principal, stored.execution_id, fields)
        return await self.wait(
            principal, stored.execution_id, fields, respond_async, current
        )

    async def decide(self, principal, execution_id, approve, reason, respond_async):
        try:
            decision = await self.approvals.decide(
                execution_id,
                principal.id,
                "APPROVED" if approve else "REJECTED",
                reason,
                workspace_id=WORKSPACE,
            )
        except CommandStoreError as exc:
            raise public_error(exc) from None
        current = projection(
            execution_id,
            decision.execution_state.public_status,
            decision.execution_state.value,
            decision.approval_status.value,
            decision.error_code,
        )
        return await self.wait(
            principal, execution_id, None, respond_async, current, decision=True
        )

    async def wait(
        self, principal, execution_id, fields, respond_async, current, *, decision=False
    ):
        if respond_async or current["status"] != "RUNNING" or self.wait_seconds == 0:
            return current
        try:
            async with asyncio.timeout(self.wait_seconds):
                while current["status"] == "RUNNING":
                    await asyncio.sleep(0.05)
                    current = await self.result(
                        principal, execution_id, fields, decision=decision
                    )
        except TimeoutError:
            pass
        return current

    async def execution_metadata(
        self,
        principal: Principal,
        execution_id: UUID | None,
        hours: int | None,
        status: str | None,
    ):
        try:
            async with self.db.connect() as conn:
                current = await resolve_principal(conn, principal.id)
                if current is None:
                    return []
                return await rows(
                    conn,
                    """
                    SELECT e.id,e.principal,e.tool_id,e.version,e.status,e.error_code,
                           e.started_at,e.finished_at,e.request_id,e.otel_trace_id,
                           c.state AS phase,a.status AS approval_status
                    FROM execution_commands c JOIN executions e ON e.id=c.execution_id
                    JOIN approvals a ON a.id=e.id
                    JOIN tools t ON t.id=c.tool_id
                    JOIN tool_versions v ON v.tool_id=c.tool_id AND v.version=c.tool_version
                    WHERE
                    """
                    + RECORD_VISIBLE
                    + """
                      AND (CAST(:execution AS uuid) IS NULL OR e.id=:execution)
                      AND (CAST(:hours AS integer) IS NULL OR e.started_at>=now()-make_interval(hours=>:hours))
                      AND (CAST(:status AS text) IS NULL OR e.status=:status)
                    ORDER BY e.started_at DESC,e.id LIMIT 100
                """,
                    workspace=WORKSPACE,
                    execution=execution_id,
                    hours=hours,
                    status=status,
                    human=current.kind == "human",
                    **scope_params(current),
                )
        except SQLAlchemyError:
            raise HTTPException(503, "EXECUTION_RESULT_UNAVAILABLE") from None

    async def audit_events(self, principal: Principal):
        try:
            async with self.db.connect() as conn:
                current = await resolve_principal(conn, principal.id)
                if current is None:
                    return []
                return await rows(
                    conn,
                    """
                    SELECT ev.id,ev.actor,ev.action,ev.resource_id,ev.created_at
                    FROM audit_events ev
                    JOIN execution_commands c ON c.execution_id=ev.resource_id
                    JOIN tools t ON t.id=c.tool_id
                    JOIN tool_versions v ON v.tool_id=c.tool_id AND v.version=c.tool_version
                    WHERE
                    """
                    + RECORD_VISIBLE
                    + """
                    ORDER BY ev.created_at DESC,ev.id DESC LIMIT 100
                """,
                    workspace=WORKSPACE,
                    human=current.kind == "human",
                    **scope_params(current),
                )
        except SQLAlchemyError:
            raise HTTPException(503, "EXECUTION_AUDIT_UNAVAILABLE") from None

    async def list_approvals(
        self, principal: Principal, execution_id: UUID | None = None
    ):
        try:
            async with self.db.connect() as conn:
                current = await resolve_principal(conn, principal.id)
                if current is None:
                    return []
                approvals = await rows(
                    conn,
                    """
                    SELECT a.id,a.requester,a.tool_id,a.version,a.status,a.requested_at,
                           a.expires_at,a.decided_by,NULL AS decision_reason,
                           e.status AS execution_status,c.workspace_id,c.customer_id,
                           c.ciphertext,c.wrapped_data_key,c.nonce,c.key_version
                    FROM approvals a JOIN execution_commands c ON c.execution_id=a.id
                    JOIN executions e ON e.id=a.id
                    JOIN tools t ON t.id=c.tool_id
                    JOIN tool_versions v ON v.tool_id=c.tool_id AND v.version=c.tool_version
                    WHERE c.workspace_id=:workspace AND c.customer_id=:customer
                      AND (CAST(:execution AS uuid) IS NULL OR a.id=:execution) AND
                    ((a.requester=:principal AND ("""
                    + POLICY
                    + """))
                     OR (:human AND a.requester<>:principal AND EXISTS (
                        SELECT 1 FROM execution_approval_entitlements q
                        WHERE q.principal=:principal AND q.customer_id=c.customer_id
                          AND q.tool_id=c.tool_id AND q.enabled
                     )))
                    ORDER BY a.requested_at DESC,a.id LIMIT 100
                """,
                    workspace=WORKSPACE,
                    execution=execution_id,
                    human=current.kind == "human",
                    **scope_params(current),
                )
        except SQLAlchemyError:
            raise HTTPException(503, "APPROVAL_STORE_UNAVAILABLE") from None
        for item in approvals:
            scope = ProtectedIntentScope.model_validate(
                {key: item[key] for key in ProtectedIntentScope.model_fields}
            )
            sealed = {key: item.pop(key) for key in SealedPayload.model_fields}
            item.pop("workspace_id")
            try:
                payload = SealedPayload.model_validate(sealed)
                intent = json.loads(
                    self.payloads.open(
                        payload, PayloadContext("command", item["id"], scope)
                    )
                )
                item.update(
                    redact(
                        {
                            key: intent[key]
                            for key in (
                                "transaction_id",
                                "service_id",
                                "amount_minor",
                                "currency",
                            )
                            if key in intent
                        }
                    )
                )
            except (PayloadUnavailable, ValidationError, ValueError):
                # Metadata remains useful even if the sealed summary is unavailable.
                continue
        return approvals

    async def result(
        self, principal: Principal, execution_id: UUID, fields, *, decision=False
    ):
        try:
            async with self.db.connect() as conn:
                async with conn.begin():
                    current = await resolve_principal(conn, principal.id)
                    if current is None:
                        raise HTTPException(404, "EXECUTION_NOT_AVAILABLE")
                    data = await rows(
                        conn,
                        """
                        SELECT c.workspace_id,c.requester,c.customer_id,c.tool_id,c.state,
                               c.contract_digest,v.contract,e.status,e.error_code,
                               a.status AS approval_status,c.next_check_at,
                               r.outcome,r.ciphertext,r.nonce,r.wrapped_data_key,r.key_version
                        FROM execution_commands c JOIN executions e ON e.id=c.execution_id
                        JOIN approvals a ON a.id=e.id
                        JOIN tools t ON t.id=c.tool_id
                        JOIN tool_versions v ON v.tool_id=c.tool_id AND v.version=c.tool_version
                        LEFT JOIN execution_receipts r ON r.execution_id=e.id
                        WHERE c.execution_id=:execution AND c.workspace_id=:workspace
                          AND c.customer_id=:customer AND
                          ((c.requester=:principal AND ("""
                        + POLICY
                        + """))
                           OR (:decision AND a.decided_by=:principal))
                        """,
                        execution=execution_id,
                        workspace=WORKSPACE,
                        decision=decision,
                        **scope_params(current),
                    )
        except SQLAlchemyError:
            raise HTTPException(503, "EXECUTION_RESULT_UNAVAILABLE") from None
        if not data:
            raise HTTPException(404, "EXECUTION_NOT_AVAILABLE")
        [item] = data
        response = projection(
            execution_id,
            item["status"],
            item["state"],
            item["approval_status"],
            item["error_code"],
        )
        if item["status"] == "UNKNOWN" and item["next_check_at"] is not None:
            response["next_check_at"] = item["next_check_at"].isoformat()
        if decision:
            return response
        contract = item["contract"]
        if sha256(canonical_json(contract)).digest() != item["contract_digest"]:
            raise HTTPException(503, "EXECUTION_RESULT_UNAVAILABLE")
        permitted = set(contract.get("output_schema", {}).get("properties", {}))
        if fields and not set(fields) <= permitted:
            raise HTTPException(422, "INVALID_ARGUMENT")
        if item["outcome"] is None or item["ciphertext"] is None:
            response["result_availability"] = (
                "NOT_READY"
                if item["status"] in {"PENDING_APPROVAL", "RUNNING", "UNKNOWN"}
                else "LEGACY_UNAVAILABLE"
            )
            return response
        if item["outcome"] != item["status"]:
            raise HTTPException(503, "EXECUTION_RESULT_UNAVAILABLE")
        try:
            scope = ProtectedIntentScope.model_validate(
                {key: item[key] for key in ProtectedIntentScope.model_fields}
            )
            sealed = SealedPayload.model_validate(
                {key: item[key] for key in SealedPayload.model_fields}
            )
            value = json.loads(
                self.payloads.open(
                    sealed, PayloadContext("receipt", execution_id, scope)
                )
            )
            if not isinstance(value, dict):
                raise ValueError
        except (PayloadUnavailable, ValidationError, ValueError):
            raise HTTPException(503, "EXECUTION_RESULT_UNAVAILABLE") from None
        safe = {key: value[key] for key in permitted if key in value}
        if not Draft202012Validator(contract["output_schema"]).is_valid(safe):
            raise HTTPException(503, "EXECUTION_RESULT_UNAVAILABLE")
        response["result"] = redact(
            {key: safe[key] for key in fields or safe if key in safe}
        )
        response["result_availability"] = "AVAILABLE"
        return response
