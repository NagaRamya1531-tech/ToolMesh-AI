from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.identity import Principal
from shared.database import execute, rows


class Catalog:
    def __init__(self, db: AsyncEngine):
        self.db = db

    async def services(self, principal: Principal):
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT s.*, count(t.id) FILTER (WHERE t.active_version IS NOT NULL
                   AND ((v.contract->>'risk' = 'read') OR (v.contract->>'risk' = 'write' AND :write) OR (v.contract->>'risk' = 'approval' AND :refund) OR :admin)) AS published_tools
                FROM services s LEFT JOIN tools t ON t.service_id=s.id
                LEFT JOIN tool_versions v ON v.tool_id=t.id AND v.version=t.active_version
                WHERE s.id = ANY(CAST(:services AS text[]))
                GROUP BY s.id ORDER BY s.id
            """,
                services=list(principal.services),
                write=principal.can_write,
                refund=principal.can_refund,
                admin=principal.can_publish,
            )

    async def registry(self, principal: Principal):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT t.id, t.service_id, t.active_version, v.version, v.contract
                FROM tools t JOIN tool_versions v ON t.id=v.tool_id ORDER BY t.id, v.version
            """,
            )

    async def publish(
        self, principal: Principal, tool_id: str, version: int, reason: str
    ):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        async with self.db.begin() as conn:
            locked = await rows(
                conn,
                "SELECT active_version FROM tools WHERE id=:id FOR UPDATE",
                id=tool_id,
            )
            candidate = await rows(
                conn,
                "SELECT version FROM tool_versions WHERE tool_id=:id AND version=:version",
                id=tool_id,
                version=version,
            )
            if not locked or not candidate:
                raise HTTPException(404, "TOOL_NOT_AVAILABLE")
            if locked[0]["active_version"] not in (None, version):
                raise HTTPException(409, "VERSION_CONFLICT")
            if locked[0]["active_version"] is None:
                await execute(
                    conn,
                    "UPDATE tools SET active_version=:version WHERE id=:id",
                    id=tool_id,
                    version=version,
                )
                await execute(
                    conn,
                    """
                    INSERT INTO publications (tool_id, version, actor, reason)
                    VALUES (:id, :version, :actor, :reason)
                """,
                    id=tool_id,
                    version=version,
                    actor=principal.id,
                    reason=reason,
                )
        return {"id": tool_id, "version": version, "status": "published"}
