"""Rank only the authorized, currently published version set."""

import asyncio
import logging
from typing import Literal

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.embeddings import MODEL_ID, Embeddings
from gateway.identity import Principal
from shared.database import execute, rows

Strategy = Literal["keyword", "vector", "hybrid"]
STRATEGIES: tuple[Strategy, ...] = ("keyword", "vector", "hybrid")
LOGGER = logging.getLogger("uvicorn.error.discovery")

POLICY = """
    t.service_id=ANY(CAST(:services AS text[]))
    AND (v.contract->>'risk'='read' OR (v.contract->>'risk'='write' AND :can_write)
        OR (v.contract->>'risk'='approval' AND EXISTS (
            SELECT 1 FROM refund_grants
            WHERE principal=:principal AND enabled AND customer_id=:customer)))
"""

# The caller provides a constant SQL fragment defining an authorized candidate set.
# Keyword and vector scoring run only against the materialized filtered relation.
RANK = """
, query_terms AS (
    SELECT CAST(string_agg(quote_literal(word), ' | ') AS tsquery) AS value
    FROM unnest(tsvector_to_array(to_tsvector('english',translate(:query,'._','  ')))) word
), keyword AS (
    SELECT c.id,row_number() OVER(ORDER BY ts_rank_cd(c.search_terms,q.value) DESC,c.id) AS rank
    FROM candidates c CROSS JOIN query_terms q
    WHERE :strategy IN ('keyword','hybrid') AND c.search_terms @@ q.value
    ORDER BY rank LIMIT 100
), semantic AS (
    SELECT c.id,row_number() OVER(ORDER BY c.embedding <=> CAST(CAST(:embedding AS text) AS vector),c.id) AS rank
    FROM candidates c
    WHERE :strategy IN ('vector','hybrid') AND c.embedding IS NOT NULL
    ORDER BY rank LIMIT 100
), scores AS (
    SELECT coalesce(k.id,s.id) AS id,
        coalesce(1.0/(60+k.rank),0)+coalesce(1.0/(60+s.rank),0) AS score
    FROM keyword k FULL JOIN semantic s ON s.id=k.id
)
SELECT c.id,c.service_id,c.version,c.contract FROM scores JOIN candidates c ON c.id=scores.id
ORDER BY scores.score DESC,c.id LIMIT :limit
"""


class Discovery:
    def __init__(self, db: AsyncEngine, embeddings: Embeddings):
        self.db = db
        self.embeddings = embeddings
        self.index_lock = asyncio.Lock()

    async def index(self):
        if self.embeddings.status != "ready":
            return
        async with self.index_lock:
            async with self.db.connect() as conn:
                pending = await rows(
                    conn,
                    """
                    SELECT t.id,v.version,v.contract->>'description' AS description
                    FROM tools t JOIN tool_versions v ON (v.tool_id,v.version)=(t.id,t.active_version)
                    LEFT JOIN tool_embeddings e ON (e.tool_id,e.version,e.model)=(t.id,v.version,:model)
                    WHERE e.tool_id IS NULL ORDER BY t.id LIMIT 100
                """,
                    model=MODEL_ID,
                )
            if not pending:
                return
            vectors = await self.embeddings.encode(
                [
                    f"{item['id'].replace('_', ' ').replace('.', ' ')} {item['description']}"
                    for item in pending
                ]
            )
            async with self.db.begin() as conn:
                for item, vector in zip(pending, vectors, strict=True):
                    await execute(
                        conn,
                        """
                        INSERT INTO tool_embeddings(tool_id,version,model,embedding)
                        VALUES(:id,:version,:model,CAST(CAST(:embedding AS text) AS vector))
                        ON CONFLICT DO NOTHING
                    """,
                        id=item["id"],
                        version=item["version"],
                        model=MODEL_ID,
                        embedding=vector,
                    )

    async def worker(self):
        while True:
            try:
                await self.index()
            except (OSError, RuntimeError, ValueError):
                LOGGER.warning("Discovery indexing could not complete")
            except Exception:
                LOGGER.warning("Discovery index database pass failed")
            await asyncio.sleep(2)

    async def tools(
        self,
        principal: Principal,
        service: str | None,
        query: str,
        limit: int,
        strategy: Strategy,
    ):
        params = {
            "services": list(principal.services),
            "principal": principal.id,
            "can_write": principal.can_write,
            "customer": principal.customer,
            "service": service or "",
            "query": query.strip(),
            "limit": limit,
            "strategy": strategy,
            "model": MODEL_ID,
            "embedding": None,
        }
        base = (
            """
            SELECT t.id,t.service_id,v.version,v.contract,v.search_terms,e.embedding
            FROM tools t JOIN tool_versions v ON (v.tool_id,v.version)=(t.id,t.active_version)
            LEFT JOIN tool_embeddings e ON (e.tool_id,e.version,e.model)=(t.id,v.version,:model)
            WHERE
        """
            + POLICY
            + " AND (:service='' OR t.service_id=:service)"
        )
        if query.strip() and strategy != "keyword":
            if self.embeddings.status != "ready":
                raise HTTPException(503, "EMBEDDING_MODEL_UNAVAILABLE")
            await self.index()
            params["embedding"] = (await self.embeddings.encode([query.strip()]))[0]
        async with self.db.connect() as conn:
            if query.strip() and strategy != "keyword":
                # Read index completeness and rank the same published authorization snapshot.
                await conn.execution_options(isolation_level="REPEATABLE READ")
            if not query.strip():
                values = await rows(
                    conn,
                    "SELECT id,service_id,version,contract FROM ("
                    + base
                    + ") active ORDER BY id LIMIT :limit",
                    **params,
                )
            else:
                if strategy != "keyword":
                    missing = await rows(
                        conn,
                        "SELECT 1 FROM ("
                        + base
                        + ") active WHERE embedding IS NULL LIMIT 1",
                        **params,
                    )
                    if missing:
                        raise HTTPException(503, "EMBEDDING_INDEX_PENDING")
                values = await rows(
                    conn,
                    "WITH candidates AS MATERIALIZED (" + base + ") " + RANK,
                    **params,
                )
        LOGGER.info(
            "Discovery strategy=%s returned_ranks=%s",
            strategy,
            list(range(1, len(values) + 1)),
        )
        return values
