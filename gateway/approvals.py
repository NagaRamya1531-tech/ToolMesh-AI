import asyncio
import hashlib
import json
from typing import Awaitable, Callable
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from gateway.access import POLICY, object_allowed, scope_params
from gateway.identity import Principal, resolve_principal
from gateway.telemetry import Telemetry
from shared.authorization_revision import legacy_approval_only
from shared.contracts import Refund, tool_contract
from shared.database import execute, rows
from shared.domain_contracts import RestartResult

Dispatch = Callable[..., Awaitable[dict]]


def intent_hash(requester: str, tool: str, version: int, arguments: dict) -> str:
    payload = json.dumps(
        [requester, tool, version, arguments], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def event(conn: AsyncConnection, actor: str, action: str, resource: UUID):
    await execute(
        conn,
        """
        INSERT INTO audit_events(actor,action,resource_id) VALUES (:actor,:action,:resource)
    """,
        actor=actor,
        action=action,
        resource=resource,
    )


async def legacy_admission_open(conn: AsyncConnection) -> bool:
    [revision] = await rows(
        conn,
        "SELECT to_regprocedure('public.protected_legacy_open()') IS NOT NULL AS enabled",
    )
    if not revision["enabled"]:
        # Older isolated approval fixtures predate the public dispatch selector.
        return True
    [gate] = await rows(conn, "SELECT public.protected_legacy_open() AS enabled")
    return bool(gate["enabled"])


class Approvals:
    def __init__(self, db: AsyncEngine, dispatch: Dispatch, telemetry: Telemetry):
        self.db = db
        self.dispatch = dispatch
        self.telemetry = telemetry

    @staticmethod
    def prefix(tool: str) -> str:
        return "REFUND" if tool == "payments.issue_refund" else "RESTART"

    @staticmethod
    async def eligible(conn, principal: Principal, tool: str, version: int, args: dict):
        if not object_allowed(principal, args):
            return []
        return await rows(
            conn,
            """
            SELECT v.contract FROM tools t JOIN tool_versions v ON (v.tool_id,v.version)=(t.id,t.active_version)
            WHERE t.id=:tool AND t.active_version=:version AND
        """
            + POLICY
            + " FOR SHARE OF t",
            tool=tool,
            version=version,
            **scope_params(principal),
        )

    async def queue(
        self,
        trace: UUID,
        principal: Principal,
        tool: str,
        version: int,
        args: dict,
        request_key: str,
    ):
        bound = intent_hash(principal.id, tool, version, args)
        async with self.db.begin() as conn:
            if not await legacy_admission_open(conn):
                raise HTTPException(503, "DURABLE_ADMISSION_DISABLED")
            await execute(
                conn,
                "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
                key=f"{principal.id}:{tool}:{request_key}",
            )
            if not await rows(
                conn,
                "SELECT id FROM executions WHERE id=:id AND status='RECEIVED' FOR UPDATE",
                id=trace,
            ):
                raise HTTPException(409, "EXECUTION_EXPIRED")
            grants = await self.eligible(conn, principal, tool, version, args)
            if not grants:
                raise HTTPException(404, "TOOL_NOT_AVAILABLE")
            prior = await rows(
                conn,
                """
                SELECT a.id,a.intent_hash,e.status,e.error_code
                FROM approvals a JOIN executions e ON e.id=a.id
                WHERE a.requester=:requester AND a.tool_id=:tool AND a.request_key=:key
            """,
                requester=principal.id,
                tool=tool,
                key=request_key,
            )
            if prior:
                if prior[0]["intent_hash"] != bound:
                    raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
                await execute(
                    conn,
                    """
                    UPDATE executions SET status='DENIED',error_code='IDEMPOTENT_REPLAY',finished_at=now() WHERE id=:id
                """,
                    id=trace,
                )
                return {
                    key: prior[0][key] for key in ("id", "status", "error_code")
                } | {"result": None, "approval_id": prior[0]["id"]}
            await execute(
                conn,
                """
                INSERT INTO approvals(id,requester,tool_id,version,customer_id,arguments,intent_hash,request_key,status)
                VALUES(:id,:requester,:tool,:version,:customer,CAST(:args AS jsonb),:hash,:key,'PENDING')
            """,
                id=trace,
                requester=principal.id,
                tool=tool,
                version=version,
                customer=args["customer_id"],
                args=json.dumps(args),
                hash=bound,
                key=request_key,
            )
            await execute(
                conn,
                """
                UPDATE executions SET status='PENDING_APPROVAL',tool_id=:tool,version=:version WHERE id=:id
            """,
                id=trace,
                tool=tool,
                version=version,
            )
            await event(
                conn, principal.id, f"{self.prefix(tool)}_APPROVAL_REQUESTED", trace
            )
        return {
            "id": trace,
            "status": "PENDING_APPROVAL",
            "error_code": None,
            "result": None,
            "approval_id": trace,
        }

    async def expire(self):
        async with self.db.begin() as conn:
            if not await legacy_admission_open(conn):
                return
            expired = await rows(
                conn,
                """
                UPDATE approvals a SET status='EXPIRED' WHERE status='PENDING' AND expires_at <= now()
            """
                + await legacy_approval_only(conn)
                + " RETURNING id,tool_id",
            )
            for row in expired:
                await execute(
                    conn,
                    "UPDATE executions SET status='EXPIRED',finished_at=now() WHERE id=:id",
                    id=row["id"],
                )
                await event(
                    conn,
                    "system",
                    f"{self.prefix(row['tool_id'])}_APPROVAL_EXPIRED",
                    row["id"],
                )

        if expired:
            self.telemetry.approval_decisions.labels(status="EXPIRED").inc(len(expired))

    async def list(self, principal: Principal):
        await self.expire()
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT a.id,a.requester,a.tool_id,a.version,a.status,a.requested_at,a.expires_at,
                    a.decided_by,a.decision_reason,e.status AS execution_status,
                    a.arguments->>'customer_id' AS customer_id,
                    a.arguments->>'transaction_id' AS transaction_id,
                    a.arguments->>'service_id' AS service_id,
                    (a.arguments->>'amount_minor')::int AS amount_minor,a.arguments->>'currency' AS currency
                FROM approvals a JOIN executions e ON e.id=a.id
                WHERE a.customer_id=:customer AND (a.requester=:requester OR :reviewer)
            """
                + await legacy_approval_only(conn)
                + " ORDER BY requested_at DESC,id LIMIT 100",
                customer=principal.customer,
                requester=principal.id,
                reviewer=principal.can_approve or principal.can_publish,
            )

    async def decide(
        self, principal: Principal, approval_id: UUID, approve: bool, reason: str
    ):
        if not principal.can_approve:
            raise HTTPException(403, "FORBIDDEN")
        await self.expire()
        async with self.db.begin() as conn:
            if not await legacy_admission_open(conn):
                raise HTTPException(503, "DURABLE_ADMISSION_DISABLED")
            values = await rows(
                conn,
                """
                SELECT *, expires_at > now() AS current FROM approvals a
                WHERE id=:id AND customer_id=:customer
            """
                + await legacy_approval_only(conn)
                + " FOR UPDATE",
                id=approval_id,
                customer=principal.customer,
            )
            if not values or values[0]["requester"] == principal.id:
                raise HTTPException(404, "APPROVAL_NOT_AVAILABLE")
            item = values[0]
            if item["status"] != "PENDING" or not item["current"]:
                raise HTTPException(409, "APPROVAL_ALREADY_DECIDED")
            status = "REJECTED"
            if approve:
                requester = await resolve_principal(conn, item["requester"])
                checks = (
                    await self.eligible(
                        conn,
                        requester,
                        item["tool_id"],
                        item["version"],
                        item["arguments"],
                    )
                    if requester
                    else []
                )
                if (
                    not checks
                    or checks[0]["contract"] != tool_contract(item["tool_id"])
                    or item["intent_hash"]
                    != intent_hash(
                        item["requester"],
                        item["tool_id"],
                        item["version"],
                        item["arguments"],
                    )
                ):
                    raise HTTPException(
                        409, f"{self.prefix(item['tool_id'])}_AUTHORIZATION_CHANGED"
                    )
                status = "APPROVED"
            await execute(
                conn,
                """
                UPDATE approvals SET status=:status,decided_by=:actor,decision_reason=:reason,decided_at=now(),
                    reconcile_after=CASE WHEN :status='APPROVED' THEN now()+interval '8 seconds' END WHERE id=:id
            """,
                status=status,
                actor=principal.id,
                reason=reason,
                id=approval_id,
            )
            if approve:
                await execute(
                    conn,
                    "UPDATE executions SET status='RUNNING' WHERE id=:id",
                    id=approval_id,
                )
            else:
                await execute(
                    conn,
                    "UPDATE executions SET status='REJECTED',finished_at=now() WHERE id=:id",
                    id=approval_id,
                )
            await event(
                conn,
                principal.id,
                f"{self.prefix(item['tool_id'])}_{status}",
                approval_id,
            )
        self.telemetry.approval_decisions.labels(status=status).inc()
        if approve:
            return await self.apply(item)
        return {
            "id": approval_id,
            "status": "REJECTED",
            "error_code": None,
            "result": None,
        }

    async def finish(self, id: UUID, status: str, code: str | None, tool: str):
        async with self.db.begin() as conn:
            changed = await rows(
                conn,
                """
                UPDATE executions SET status=:status,error_code=:code,finished_at=now()
                WHERE id=:id AND (status='RUNNING' OR (status='UNKNOWN' AND :status='SUCCEEDED'))
                RETURNING id
            """,
                id=id,
                status=status,
                code=code,
            )
            if changed:
                await event(conn, "system", f"{self.prefix(tool)}_{status}", id)

    async def apply(self, item: dict):
        refund_id = item["id"]
        code = None
        result = None
        try:
            payload = await self.dispatch(
                item["tool_id"], item["arguments"], refund_id=refund_id
            )
            result = self.validate(item, payload)
            status = "SUCCEEDED"
        except HTTPException as exc:
            status = "FAILED" if exc.status_code in (404, 409) else "UNKNOWN"
            code = str(exc.detail)
        except (TimeoutError, httpx.HTTPError, ValueError, RuntimeError):
            status, code = "UNKNOWN", "UNKNOWN_OUTCOME"
        await self.finish(refund_id, status, code, item["tool_id"])
        return {"id": refund_id, "status": status, "error_code": code, "result": result}

    @staticmethod
    def validate(item: dict, payload: dict):
        model = Refund if item["tool_id"] == "payments.issue_refund" else RestartResult
        result = model.model_validate_json(json.dumps(payload)).model_dump(mode="json")
        if (
            result["id"] != str(item["id"])
            or result["status"] != "SUCCEEDED"
            or any(result.get(k) != value for k, value in item["arguments"].items())
        ):
            raise ValueError("Approval receipt mismatch")
        return result

    async def reconcile(self):
        await self.expire()
        async with self.db.begin() as conn:
            if not await legacy_admission_open(conn):
                return
            candidates = await rows(
                conn,
                """
                SELECT a.id,a.tool_id,a.arguments,a.customer_id FROM approvals a JOIN executions e ON e.id=a.id
                WHERE a.status='APPROVED' AND e.status IN ('RUNNING','UNKNOWN') AND a.reconcile_after<=now()
            """
                + await legacy_approval_only(conn)
                + " ORDER BY a.reconcile_after LIMIT 10 FOR UPDATE OF a SKIP LOCKED",
            )
            for item in candidates:
                await execute(
                    conn,
                    """
                    UPDATE approvals SET reconcile_after=now()+interval '10 seconds' WHERE id=:id
                """,
                    id=item["id"],
                )
        for item in candidates:
            try:
                if item["tool_id"] == "payments.issue_refund":
                    payload = await self.dispatch(
                        "payments.get_refund_status",
                        {
                            "refund_id": str(item["id"]),
                            "customer_id": item["customer_id"],
                        },
                    )
                else:
                    payload = await self.dispatch(
                        "it.get_restart_receipt",
                        {**item["arguments"], "operation_id": str(item["id"])},
                    )
                self.validate(item, payload)
            except (
                HTTPException,
                httpx.HTTPError,
                TimeoutError,
                ValueError,
                RuntimeError,
            ):
                await self.finish(
                    item["id"], "UNKNOWN", "UNKNOWN_OUTCOME", item["tool_id"]
                )
            else:
                await self.finish(item["id"], "SUCCEEDED", None, item["tool_id"])

    async def worker(self):
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The next pass retries interrupted database work.
                import logging

                logging.getLogger(__name__).warning("Refund reconciliation pass failed")
            await asyncio.sleep(2)
