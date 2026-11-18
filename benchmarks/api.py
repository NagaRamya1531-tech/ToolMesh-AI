import csv
from io import StringIO
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field

from benchmarks.evaluation import Evaluations
from gateway.discovery import STRATEGIES
from gateway.embeddings import MODEL_ID
from gateway.identity import Identity


class StartEvaluation(BaseModel):
    catalog_size: Annotated[int, Field(ge=60, le=10000)] = 1000
    case_count: Annotated[int, Field(ge=10, le=1000)] = 120
    split: Literal["test", "tuning"] = "test"


def build_evaluation_api(identity: Identity, evaluator: Evaluations) -> APIRouter:
    api = APIRouter(prefix="/api/v1/evaluations")

    async def admin(authorization: Annotated[str, Header()] = ""):
        principal = await identity.principal(authorization)
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        return principal

    async def get(run_id: UUID):
        matches = await evaluator.list(run_id)
        if not matches:
            raise HTTPException(404, "EVALUATION_NOT_AVAILABLE")
        return matches[0]

    @api.get("")
    async def listed(_=Depends(admin)):
        return {
            "model": {"id": MODEL_ID, "status": evaluator.embeddings.status},
            "runs": await evaluator.list(),
        }

    @api.post("/runs", status_code=202)
    async def start(body: StartEvaluation, principal=Depends(admin)):
        return await evaluator.enqueue(
            principal.id, body.catalog_size, body.case_count, body.split
        )

    @api.get("/runs/{run_id}")
    async def detail(run_id: UUID, _=Depends(admin)):
        run = await get(run_id)
        runs = await evaluator.cases(run_id) if run["status"] == "SUCCEEDED" else []
        failed = [
            row
            for row in runs
            if row["expected"]
            and any(row["measures"][strategy]["recall"] < 1 for strategy in STRATEGIES)
        ]
        negatives = [
            row for row in runs if not row["expected"] and any(row["rankings"].values())
        ]
        return {
            "run": run,
            "misses": failed[:25],
            "negative_examples": negatives[:10],
            "miss_count": len(failed),
            "negative_return_count": len(negatives),
        }

    @api.get("/runs/{run_id}/export")
    async def export(
        run_id: UUID, format: Literal["json", "csv"] = "json", _=Depends(admin)
    ):
        run = await get(run_id)
        if run["status"] != "SUCCEEDED":
            raise HTTPException(409, "EVALUATION_NOT_COMPLETE")
        cases = await evaluator.cases(run_id)
        filename = f"toolmesh-evaluation-{run_id}.{format}"
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        if format == "json":
            from fastapi.encoders import jsonable_encoder
            from fastapi.responses import JSONResponse

            return JSONResponse(
                jsonable_encoder({"run": run, "cases": cases}), headers=headers
            )
        target = StringIO(newline="")
        writer = csv.writer(target)
        writer.writerow(
            [
                "id",
                "domain",
                "kind",
                "query",
                "strategy",
                "expected",
                "ranked_ids",
                "recall_at_5",
                "precision_at_5",
                "reciprocal_rank",
                "latency_ms",
                "schema_bytes",
            ]
        )
        for row in cases:
            for strategy in STRATEGIES:
                metrics = row["measures"][strategy]
                writer.writerow(
                    [
                        row["id"],
                        row["domain"],
                        row["kind"],
                        row["query"],
                        strategy,
                        " ".join(row["expected"]),
                        " ".join(row["rankings"][strategy]),
                        metrics["recall"],
                        metrics["precision"],
                        metrics["reciprocal_rank"],
                        metrics["latency_ms"],
                        metrics["schema_bytes"],
                    ]
                )
        return Response(target.getvalue(), media_type="text/csv", headers=headers)

    return api
