"""Bounded, durable scripted LangGraph workflows. Tools always use the normal gateway."""

import asyncio
import json
import logging
from typing import Annotated, Any, TypedDict
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from gateway.identity import Identity, Principal, resolve_principal
from gateway.runtime import Runtime
from shared.base_contracts import Identifier
from shared.database import execute, rows
from shared.domain_contracts import CompareWindow, LeaveRequest

LOGGER = logging.getLogger(__name__)
PLANS = {
    "support": (
        "support.get_customer",
        "payments.list_transactions",
        "payments.check_duplicate_payment",
    ),
    "finance": (
        "analytics.get_sales_metrics",
        "analytics.get_refund_metrics",
        "analytics.compare_periods",
        "analytics.generate_report",
    ),
    "it": (
        "it.check_service_health",
        "it.get_system_logs",
        "it.create_incident",
        "it.restart_service",
        "it.check_service_health",
    ),
    "hr": ("hr.get_leave_balance", "hr.request_leave", "communication.notify_leave"),
}


class StartRun(BaseModel):
    arguments: dict
    request_id: UUID


class GraphState(TypedDict):
    step: int
    status: str
    steps: list[dict]
    approval_id: str | None


class SupportArgs(BaseModel):
    order_id: Identifier


class ITArgs(BaseModel):
    service_id: Identifier


class AgentRuns:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.db = runtime.db

    def validate(self, principal: Principal, arguments: dict):
        contracts: dict[str, type[BaseModel]] = {
            "support": SupportArgs,
            "it": ITArgs,
            "finance": CompareWindow,
            "hr": LeaveRequest,
        }
        contract = contracts.get(principal.profile)
        if contract is None:
            raise HTTPException(403, "PROFILE_UNAVAILABLE")
        if set(arguments) - (set(contract.model_fields) - {"customer_id"}):
            raise HTTPException(422, "INVALID_SCENARIO_ARGUMENTS")
        try:
            values = (
                arguments | {"customer_id": principal.customer}
                if "customer_id" in contract.model_fields
                else arguments
            )
            return contract.model_validate_json(json.dumps(values)).model_dump(
                mode="json"
            )
        except ValidationError:
            raise HTTPException(422, "INVALID_SCENARIO_ARGUMENTS") from None

    async def start(self, principal: Principal, body: StartRun):
        arguments = self.validate(principal, body.arguments)
        async with self.db.begin() as conn:
            await execute(
                conn,
                """
                INSERT INTO agent_runs(id,principal,profile,arguments)
                VALUES(:id,:principal,:profile,CAST(:args AS jsonb)) ON CONFLICT DO NOTHING
            """,
                id=body.request_id,
                principal=principal.id,
                profile=principal.profile,
                args=json.dumps(arguments),
            )
        found = await self.details(principal, body.request_id)
        if found["arguments"] != arguments:
            raise HTTPException(409, "SCENARIO_CONFLICT")
        return found

    async def list_runs(self, principal: Principal, run_id: UUID | None = None):
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT id,principal,profile,arguments,steps,next_step,status,approval_id,created_at,touched_at
                FROM agent_runs WHERE principal=:principal AND (CAST(:id AS uuid) IS NULL OR id=:id)
                ORDER BY created_at DESC LIMIT 50
            """,
                principal=principal.id,
                id=run_id,
            )

    async def details(self, principal: Principal, id: UUID):
        found = await self.list_runs(principal, id)
        if not found:
            raise HTTPException(404, "SCENARIO_NOT_AVAILABLE")
        return found[0]

    async def resume(self, principal: Principal, id: UUID):
        found = await self.details(principal, id)
        if found["status"] != "WAITING_FOR_APPROVAL":
            raise HTTPException(409, "SCENARIO_NOT_WAITING")
        executions = await self.runtime.executions(principal, found["approval_id"])
        status = executions[0]["status"] if executions else None
        if status not in {"SUCCEEDED", "REJECTED", "EXPIRED", "FAILED"}:
            raise HTTPException(409, "APPROVAL_NOT_TERMINAL")
        async with self.db.begin() as conn:
            await execute(
                conn,
                """
                UPDATE agent_runs SET status=:status,touched_at=now()
                WHERE id=:id AND status='WAITING_FOR_APPROVAL'
            """,
                status="QUEUED" if status == "SUCCEEDED" else "STOPPED",
                id=id,
            )
        return await self.details(principal, id)

    async def worker(self):
        while True:
            try:
                async with self.db.begin() as conn:
                    await execute(
                        conn,
                        """
                        UPDATE agent_runs SET status='INTERRUPTED',touched_at=now()
                        WHERE status='RUNNING' AND touched_at<now()-interval '45 seconds'
                    """,
                    )
                    values = await rows(
                        conn,
                        """
                        UPDATE agent_runs SET status='RUNNING',touched_at=now()
                        WHERE id=(SELECT id FROM agent_runs WHERE status='QUEUED'
                            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                        RETURNING *
                    """,
                    )
                if values:
                    job = values[0]
                    try:
                        async with asyncio.timeout(35):
                            await self.process(job)
                    except Exception:
                        LOGGER.warning("Scripted run interrupted")
                        async with self.db.begin() as conn:
                            await execute(
                                conn,
                                "UPDATE agent_runs SET status='INTERRUPTED',touched_at=now() WHERE id=:id",
                                id=job["id"],
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning("Agent worker pass interrupted")
            await asyncio.sleep(0.5)

    async def process(self, job: dict):
        plan = PLANS[job["profile"]]

        async def step(state: GraphState):
            index = state["step"]
            tool = plan[index]
            async with self.db.connect() as conn:
                principal = await resolve_principal(conn, job["principal"])
            outcome: dict[str, Any]
            if principal is None:
                outcome = {"status": "DENIED", "error_code": "PRINCIPAL_UNAVAILABLE"}
            else:
                args = self.step_arguments(
                    principal, job["arguments"], tool, state["steps"]
                )
                # Browse the eligible published service before resolving a specific immutable contract.
                discovered = await self.runtime.tools(
                    principal, tool.split(".")[0], limit=50
                )
                candidate = next(
                    (item for item in discovered if item["id"] == tool), None
                )
                if candidate is None:
                    outcome = {"status": "DENIED", "error_code": "TOOL_NOT_AVAILABLE"}
                else:
                    outcome = await self.runtime.invoke(
                        principal,
                        tool,
                        args,
                        candidate["version"],
                        idempotency_key=f"agent-run:{job['id']}:{index}",
                    )

            detail: dict[str, Any] = outcome.get("result") or {}
            summary = {
                key: detail[key]
                for key in (
                    "id",
                    "available_days",
                    "status",
                    "delta_minor",
                    "total_minor",
                    "duplicate_capture_detected",
                    "groups",
                )
                if key in detail
            }
            steps = [
                *state["steps"],
                {
                    "tool": tool,
                    "id": str(outcome["id"]) if outcome.get("id") else None,
                    "status": outcome["status"],
                    "error_code": outcome.get("error_code"),
                    "summary": summary,
                },
            ]
            if outcome["status"] == "PENDING_APPROVAL":
                status = "WAITING_FOR_APPROVAL"
            elif outcome["status"] != "SUCCEEDED":
                status = "STOPPED"
            elif index + 1 == len(plan):
                status = "SUCCEEDED"
            else:
                status = "RUNNING"
            new = outcome.get("approval_id") or state["approval_id"]
            approval_id = str(new) if new else None
            async with self.db.begin() as conn:
                await execute(
                    conn,
                    """
                    UPDATE agent_runs SET status=:status,steps=CAST(:steps AS jsonb),next_step=:step,
                      approval_id=:approval,touched_at=now() WHERE id=:id AND status='RUNNING'
                """,
                    status=status,
                    steps=json.dumps(steps),
                    step=index + 1,
                    approval=UUID(approval_id) if approval_id else None,
                    id=job["id"],
                )
            return {
                "step": index + 1,
                "status": status,
                "steps": steps,
                "approval_id": approval_id,
            }

        graph = StateGraph(GraphState)
        graph.add_node("invoke", step)
        graph.add_edge(START, "invoke")
        graph.add_conditional_edges(
            "invoke", lambda state: "invoke" if state["status"] == "RUNNING" else END
        )
        await graph.compile().ainvoke(
            {
                "step": job["next_step"],
                "status": "RUNNING",
                "steps": job["steps"],
                "approval_id": str(job["approval_id"]) if job["approval_id"] else None,
            },
            config={"recursion_limit": len(plan) + 2},
        )

    @staticmethod
    def step_arguments(
        principal: Principal, values: dict, tool: str, steps: list[dict]
    ):
        args: dict[str, Any] = {"customer_id": principal.customer}
        if tool == "payments.check_duplicate_payment":
            args["order_id"] = values["order_id"]
        elif principal.profile == "finance":
            args.update(
                {
                    key: value
                    for key, value in values.items()
                    if key in {"start_date", "end_date"}
                    or (
                        tool == "analytics.compare_periods"
                        and key.startswith("previous_")
                    )
                }
            )
        elif principal.profile == "it":
            args["service_id"] = values["service_id"]
            if tool == "it.get_system_logs":
                args.update(level="ERROR", limit=10)
            elif tool == "it.create_incident":
                args["title"] = "Scripted sandbox service investigation"
        elif principal.profile == "hr":
            args["employee_id"] = values["employee_id"]
            if tool == "hr.request_leave":
                args.update(
                    start_date=values["start_date"], end_date=values["end_date"]
                )
            elif tool == "communication.notify_leave":
                args["leave_id"] = steps[-1]["summary"]["id"]
        return args


def build_agent_api(identity: Identity, runs: AgentRuns):
    router = APIRouter(prefix="/api/v1/playground/runs")

    async def caller(authorization: Annotated[str, Header()] = ""):
        return await identity.principal(authorization)

    @router.get("")
    async def list_runs(principal=Depends(caller)):
        return await runs.list_runs(principal)

    @router.post("", status_code=202)
    async def create(body: StartRun, principal=Depends(caller)):
        return await runs.start(principal, body)

    @router.get("/{id}")
    async def detail(id: UUID, principal=Depends(caller)):
        return await runs.details(principal, id)

    @router.post("/{id}/resume", status_code=202)
    async def resume(id: UUID, principal=Depends(caller)):
        return await runs.resume(principal, id)

    return router
