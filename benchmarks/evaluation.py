"""Durable worker for comparative retrieval evaluation against PostgreSQL."""

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from benchmarks.dataset import DATASET, Case, canonical, corpus, fingerprint, labeled
from gateway.discovery import RANK, STRATEGIES
from gateway.embeddings import MODEL_ID, Embeddings
from shared.database import execute, rows

LOGGER = logging.getLogger(__name__)
LOCK = 680906
K = 5
CONFIG = {
    "dataset": DATASET,
    "model": MODEL_ID,
    "k": K,
    "rrf_k": 60,
    "candidate_limit": 100,
    "seed": 42,
}

BENCH_RANK = (
    """
WITH candidates AS MATERIALIZED (
    SELECT id,domain AS service_id,1 AS version,definition AS contract,search_terms,embedding
    FROM evaluation_documents
    WHERE run_id=:id AND NOT restricted
      AND (:kind<>'denied' OR domain<>:domain)
)
"""
    + RANK
)


def case_metrics(case: Case, result: list[dict], latency: float) -> dict:
    expected = set(case.expected)
    returned = [row["id"] for row in result]
    hits = expected.intersection(returned)
    first = next(
        (rank for rank, identifier in enumerate(returned, 1) if identifier in expected),
        None,
    )
    return {
        "recall": len(hits) / len(expected) if expected else None,
        "precision": len(hits) / K,
        "reciprocal_rank": 1 / first if first else 0,
        "latency_ms": round(latency, 4),
        "schema_bytes": len(canonical([row["contract"] for row in result])),
    }


def aggregate(cases: list[dict]) -> dict:
    metrics = {}
    for strat in STRATEGIES:
        positives = [row["measures"][strat] for row in cases if row["expected"]]
        negatives = [row for row in cases if not row["expected"]]
        latencies = sorted(row["measures"][strat]["latency_ms"] for row in cases)
        metrics[strat] = {
            "recall_at_5": sum(m["recall"] for m in positives) / len(positives)
            if positives
            else None,
            "precision_at_5": sum(row["measures"][strat]["precision"] for row in cases)
            / len(cases),
            "mrr": sum(m["reciprocal_rank"] for m in positives) / len(positives)
            if positives
            else None,
            "negative_return_rate": sum(
                bool(row["rankings"][strat]) for row in negatives
            )
            / len(negatives)
            if negatives
            else None,
            "latency_p50_ms": latencies[(len(latencies) - 1) // 2],
            "latency_p95_ms": latencies[max(0, (95 * len(latencies) + 99) // 100 - 1)],
            "mean_retrieved_schema_bytes": sum(
                row["measures"][strat]["schema_bytes"] for row in cases
            )
            / len(cases),
            "positive_cases": len(positives),
            "negative_cases": len(negatives),
        }
    return metrics


class Evaluations:
    def __init__(self, db: AsyncEngine, embeddings: Embeddings):
        self.db = db
        self.embeddings = embeddings

    async def enqueue(self, actor: str, catalog_size: int, case_count: int, split: str):
        if self.embeddings.status != "ready":
            raise HTTPException(503, "EMBEDDING_MODEL_UNAVAILABLE")
        async with self.db.begin() as conn:
            result = await rows(
                conn,
                """
                INSERT INTO evaluation_runs(id,actor,catalog_size,case_count,seed,configuration)
                VALUES(:id,:actor,:size,:cases,42,CAST(:configuration AS jsonb))
                ON CONFLICT DO NOTHING RETURNING id,status
            """,
                id=uuid4(),
                actor=actor,
                size=catalog_size,
                cases=case_count,
                configuration=json.dumps(CONFIG | {"split": split}),
            )
        if not result:
            raise HTTPException(409, "EVALUATION_ALREADY_ACTIVE")
        return result[0]

    async def list(self, run_id: UUID | None = None):
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT * FROM evaluation_runs WHERE CAST(:id AS uuid) IS NULL OR id=:id
                ORDER BY requested_at DESC LIMIT 50
            """,
                id=run_id,
            )

    async def cases(self, run_id: UUID, limit: int = 1000):
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT id,domain,kind,query,expected,rankings,measures FROM evaluation_cases
                WHERE run_id=:id ORDER BY id LIMIT :limit
            """,
                id=run_id,
                limit=limit,
            )

    async def worker(self):
        while True:
            if self.embeddings.status == "ready":
                try:
                    async with self.db.connect() as guard:
                        locked = (
                            await rows(
                                guard,
                                "SELECT pg_try_advisory_lock(:lock) AS acquired",
                                lock=LOCK,
                            )
                        )[0]["acquired"]
                        await guard.commit()
                        if locked:
                            try:
                                await self.claim_and_run()
                            finally:
                                await execute(
                                    guard, "SELECT pg_advisory_unlock(:lock)", lock=LOCK
                                )
                                await guard.commit()
                except Exception:
                    LOGGER.warning("Evaluation worker pass interrupted")
            await asyncio.sleep(1)

    async def claim_and_run(self):
        async with self.db.begin() as conn:
            result = await rows(
                conn,
                """
                UPDATE evaluation_runs SET status='RUNNING',stage='INDEXING',processed=0,
                    started_at=coalesce(started_at,now())
                WHERE id=(SELECT id FROM evaluation_runs WHERE status IN ('QUEUED','RUNNING')
                    ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *
            """,
            )
            if not result:
                return
            job = result[0]
            await self.clear_transient(conn, job["id"], cases=True)
        try:
            await self.run(job)
        except Exception:
            LOGGER.warning("Evaluation run failed")
            async with self.db.begin() as conn:
                await self.clear_transient(conn, job["id"], cases=True)
                await execute(
                    conn,
                    """
                    UPDATE evaluation_runs SET status='FAILED',stage='FAILED',
                        error_code='EVALUATION_FAILED',finished_at=now() WHERE id=:id
                """,
                    id=job["id"],
                )

    @staticmethod
    async def clear_transient(conn: AsyncConnection, run_id: UUID, cases: bool):
        await execute(
            conn, "DELETE FROM evaluation_documents WHERE run_id=:id", id=run_id
        )
        if cases:
            await execute(
                conn, "DELETE FROM evaluation_cases WHERE run_id=:id", id=run_id
            )

    async def run(self, job: dict):
        run_id = job["id"]
        docs = corpus(job["catalog_size"], job["seed"])
        questions = labeled(
            docs, job["case_count"], job["seed"], job["configuration"]["split"]
        )
        embed_start = time.perf_counter()
        encoding_ms = 0.0
        for offset in range(0, len(docs), 256):
            batch = docs[offset : offset + 256]
            encode_start = time.perf_counter()
            vectors = await self.embeddings.encode(
                [
                    f"{doc['definition']['name']} {doc['definition']['description']}"
                    for doc in batch
                ]
            )
            encoding_ms += (time.perf_counter() - encode_start) * 1000
            params = [
                {
                    "run": run_id,
                    "id": doc["id"],
                    "domain": doc["domain"],
                    "restricted": doc["restricted"],
                    "definition": canonical(doc["definition"]).decode(),
                    "embedding": vector,
                }
                for doc, vector in zip(batch, vectors, strict=True)
            ]
            async with self.db.begin() as conn:
                await conn.execute(
                    text("""
                    INSERT INTO evaluation_documents(run_id,id,domain,restricted,definition,embedding)
                    VALUES(:run,:id,:domain,:restricted,CAST(:definition AS jsonb),CAST(CAST(:embedding AS text) AS vector))
                """),
                    params,
                )
                await execute(
                    conn,
                    "UPDATE evaluation_runs SET processed=:count WHERE id=:id",
                    id=run_id,
                    count=offset + len(batch),
                )
            # Hand the loop back to other workers and API tasks between batches.
            await asyncio.sleep(0)
        build_ms = (time.perf_counter() - embed_start) * 1000
        async with self.db.begin() as conn:
            await execute(
                conn,
                "UPDATE evaluation_runs SET stage='RANKING',processed=0 WHERE id=:id",
                id=run_id,
            )
        queries_start = time.perf_counter()
        embeddings = await self.embeddings.encode([item.query for item in questions])
        query_ms = (time.perf_counter() - queries_start) * 1000
        results: list[dict[str, Any]] = []
        for start in range(0, len(questions), 20):
            async with self.db.begin() as conn:
                for i in range(start, min(start + 20, len(questions))):
                    case = questions[i]
                    measures = {}
                    ranked = {}
                    # Rotate strategy order to spread cache-warm effects.
                    order = STRATEGIES[i % 3 :] + STRATEGIES[: i % 3]
                    for strategy in order:
                        clock = time.perf_counter()
                        found = await rows(
                            conn,
                            BENCH_RANK,
                            id=run_id,
                            query=case.query,
                            strategy=strategy,
                            embedding=embeddings[i],
                            domain=case.domain,
                            kind=case.kind,
                            limit=K,
                        )
                        measures[strategy] = case_metrics(
                            case, found, (time.perf_counter() - clock) * 1000
                        )
                        ranked[strategy] = [row["id"] for row in found]
                    result = {
                        "id": case.id,
                        "domain": case.domain,
                        "kind": case.kind,
                        "query": case.query,
                        "expected": list(case.expected),
                        "rankings": ranked,
                        "measures": measures,
                    }
                    results.append(result)
                    await execute(
                        conn,
                        """
                        INSERT INTO evaluation_cases(run_id,id,domain,kind,query,expected,rankings,measures)
                        VALUES(:run,:id,:domain,:kind,:query,CAST(:expected AS jsonb),CAST(:rankings AS jsonb),CAST(:measures AS jsonb))
                    """,
                        run=run_id,
                        **(
                            result
                            | {
                                key: json.dumps(result[key])
                                for key in ("expected", "rankings", "measures")
                            }
                        ),
                    )
                await execute(
                    conn,
                    "UPDATE evaluation_runs SET processed=:count WHERE id=:id",
                    id=run_id,
                    count=len(results),
                )
        by_domain = defaultdict(list)
        for result in results:
            by_domain[result["domain"]].append(result)
        all_allowed = [doc["definition"] for doc in docs if not doc["restricted"]]
        summary = {
            "strategies": aggregate(results),
            "domains": {
                domain: aggregate(cases) for domain, cases in by_domain.items()
            },
            "dataset_fingerprint": fingerprint(docs, questions),
            "all_allowed_schema_bytes": len(canonical(all_allowed)),
            "eligible_definitions": len(all_allowed),
            "embedding_cpu_wall_ms": round(encoding_ms, 3),
            "query_embedding_wall_ms": round(query_ms, 3),
            "index_build_wall_ms": round(build_ms, 3),
            "full_schema_llm_baseline": "not_run",
            "latency_scope": "PostgreSQL query round trip; query encoding reported separately",
        }
        async with self.db.begin() as conn:
            await self.clear_transient(conn, run_id, cases=False)
            await execute(
                conn,
                """
                UPDATE evaluation_runs SET status='SUCCEEDED',stage='COMPLETE',summary=CAST(:summary AS jsonb),
                    finished_at=now() WHERE id=:id
            """,
                summary=json.dumps(summary),
                id=run_id,
            )
