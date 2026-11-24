import json
from uuid import UUID

from fastapi import HTTPException

from gateway.identity import Principal
from gateway.runtime import Runtime
from shared.database import execute, rows

SELECT_RUN = """
    SELECT w.id,w.order_id,w.customer_id,w.started_by,w.evidence,w.ticket_id,w.refund_id,
           w.notification_id,w.status,w.created_at,w.updated_at,e.status AS refund_status,a.arguments->>'transaction_id' AS refund_transaction
    FROM duplicate_workflows w LEFT JOIN executions e ON e.id=w.refund_id LEFT JOIN approvals a ON a.id=w.refund_id
    WHERE w.customer_id=:customer
"""


class DuplicateWorkflow:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    @staticmethod
    def authorize(principal: Principal):
        if principal.id not in {
            "support-agent",
            "finance-agent",
            "refund-approver",
            "platform-admin",
        }:
            raise HTTPException(403, "FORBIDDEN")

    async def list(self, principal: Principal, run_id: UUID | None = None):
        self.authorize(principal)
        async with self.runtime.db.connect() as conn:
            return await rows(
                conn,
                SELECT_RUN
                + """
                AND (CAST(:id AS uuid) IS NULL OR w.id=CAST(:id AS uuid)) ORDER BY w.created_at DESC LIMIT 50
            """,
                customer=principal.customer,
                id=run_id,
            )

    async def record(self, principal: Principal, run_id: UUID):
        records = await self.list(principal, run_id)
        if not records:
            raise HTTPException(404, "WORKFLOW_NOT_AVAILABLE")
        return records[0]

    async def start(self, principal: Principal, run_id: UUID, order: str):
        if principal.id != "support-agent":
            raise HTTPException(403, "FORBIDDEN")
        async with self.runtime.db.begin() as conn:
            await execute(
                conn,
                """
                INSERT INTO duplicate_workflows(id,customer_id,order_id,started_by)
                VALUES (:id,:customer,:order,:actor) ON CONFLICT (id) DO NOTHING
            """,
                id=run_id,
                customer=principal.customer,
                order=order,
                actor=principal.id,
            )
        record = await self.record(principal, run_id)
        if record["order_id"] != order:
            raise HTTPException(409, "WORKFLOW_CONFLICT")
        if record["status"] != "STARTED":
            return record

        arguments = {"customer_id": principal.customer, "order_id": order}
        check = await self.runtime.invoke(
            principal, "payments.check_duplicate_payment", arguments
        )
        evidence = self.success(check)
        if not evidence["duplicate_capture_detected"]:
            async with self.runtime.db.begin() as conn:
                await execute(
                    conn,
                    """
                    UPDATE duplicate_workflows SET status='NO_DUPLICATE',evidence=CAST(:evidence AS jsonb),updated_at=now()
                    WHERE id=:id AND status='STARTED'
                """,
                    id=run_id,
                    evidence=json.dumps({"groups": [], "trace_id": check["id"]}),
                )
        else:
            groups = evidence["groups"]
            if len(groups) != 1 or not 2 <= len(groups[0]) <= 10:
                raise HTTPException(409, "EVIDENCE_REQUIRES_REVIEW")
            group = groups[0]
            await self.runtime.schema(principal, "support.create_ticket", None)
            await self.runtime.quota(principal, "support.create_ticket")
            ticket = await self.runtime.downstream(
                "support.open_duplicate_ticket",
                {
                    **arguments,
                    "ticket_id": str(run_id),
                    "transaction_ids": group,
                },
            )
            if (
                ticket.get("id") != str(run_id)
                or ticket.get("customer_id") != principal.customer
                or ticket.get("order_id") != order
            ):
                raise HTTPException(502, "UPSTREAM_ERROR")
            async with self.runtime.db.begin() as conn:
                await execute(
                    conn,
                    """
                    UPDATE duplicate_workflows SET ticket_id=:id,status='TICKET_OPEN',
                    evidence=CAST(:evidence AS jsonb),updated_at=now() WHERE id=:id AND status='STARTED'
                """,
                    id=run_id,
                    evidence=json.dumps({"groups": [group], "trace_id": check["id"]}),
                )
        return await self.record(principal, run_id)

    async def request(self, principal: Principal, run_id: UUID, transaction_id: str):
        if not principal.can_refund:
            raise HTTPException(403, "FORBIDDEN")
        record = await self.record(principal, run_id)
        if record["refund_id"]:
            if record[
                "refund_transaction"
            ] is None and await self.runtime.is_durable_execution(record["refund_id"]):
                matches = await self.runtime.require_protected().list_approvals(
                    principal, record["refund_id"]
                )
                if not matches or matches[0].get("transaction_id") is None:
                    raise HTTPException(404, "APPROVAL_NOT_AVAILABLE")
                record["refund_transaction"] = matches[0]["transaction_id"]
            if record["refund_transaction"] != transaction_id:
                raise HTTPException(409, "WORKFLOW_CONFLICT")
            return record
        if (
            record["status"] != "TICKET_OPEN"
            or transaction_id not in record["evidence"]["groups"][0]
        ):
            raise HTTPException(409, "WORKFLOW_NOT_READY")
        args = {"customer_id": principal.customer, "transaction_id": transaction_id}
        capture = self.success(
            await self.runtime.invoke(principal, "payments.get_transaction", args)
        )
        if capture["order_id"] != record["order_id"]:
            raise HTTPException(409, "TICKET_REFUND_MISMATCH")
        pending = await self.runtime.invoke(
            principal,
            "payments.issue_refund",
            {
                **args,
                "amount_minor": capture["amount_minor"],
                "currency": capture["currency"],
            },
            idempotency_key=f"workflow:{run_id}",
        )
        if not pending.get("approval_id"):
            raise HTTPException(409, pending["error_code"] or "WORKFLOW_NOT_READY")
        async with self.runtime.db.begin() as conn:
            await execute(
                conn,
                """
                UPDATE duplicate_workflows SET status='REFUND_REQUESTED',refund_id=:refund,updated_at=now()
                WHERE id=:id AND refund_id IS NULL
            """,
                id=run_id,
                refund=pending["approval_id"],
            )
        linked = await self.record(principal, run_id)
        if linked["refund_id"] == UUID(str(pending["approval_id"])):
            linked["refund_transaction"] = transaction_id
        return linked

    async def complete(self, principal: Principal, run_id: UUID):
        if principal.id != "support-agent":
            raise HTTPException(403, "FORBIDDEN")
        record = await self.record(principal, run_id)
        if record["status"] == "RESOLVED":
            return record
        if record["refund_status"] != "SUCCEEDED":
            raise HTTPException(409, "REFUND_NOT_CONFIRMED")
        args = {
            "customer_id": principal.customer,
            "refund_id": str(record["refund_id"]),
        }
        notification = self.success(
            await self.runtime.invoke(
                principal, "communication.send_notification", args
            )
        )
        self.success(
            await self.runtime.invoke(
                principal,
                "support.resolve_ticket",
                {
                    **args,
                    "ticket_id": str(record["ticket_id"]),
                },
            )
        )
        async with self.runtime.db.begin() as conn:
            await execute(
                conn,
                """
                UPDATE duplicate_workflows SET status='RESOLVED',notification_id=:notification,updated_at=now()
                WHERE id=:id AND status='REFUND_REQUESTED'
            """,
                id=run_id,
                notification=UUID(notification["id"]),
            )
        return await self.record(principal, run_id)

    @staticmethod
    def success(result: dict):
        if result["status"] != "SUCCEEDED" or not result["result"]:
            raise HTTPException(409, result["error_code"] or "WORKFLOW_NOT_READY")
        return result["result"]
