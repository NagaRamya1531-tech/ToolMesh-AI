from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.access import POLICY, scope_params
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
                SELECT s.*,count(t.id) FILTER (WHERE t.active_version IS NOT NULL AND (:admin OR (
            """
                + POLICY
                + """
                ))) AS published_tools
                FROM services s LEFT JOIN tools t ON t.service_id=s.id
                LEFT JOIN tool_versions v ON v.tool_id=t.id AND v.version=t.active_version
                WHERE s.id=ANY(CAST(:services AS text[]))
                GROUP BY s.id ORDER BY s.id
            """,
                admin=principal.can_publish,
                **scope_params(principal),
            )

    async def registry(self, principal: Principal):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT t.id, t.service_id, t.active_version, t.lifecycle, v.version, v.contract
                FROM tools t JOIN tool_versions v ON t.id=v.tool_id ORDER BY t.id, v.version DESC
            """,
            )

    async def publish(
        self, principal: Principal, tool_id: str, version: int, reason: str
    ):
        return await self.release(principal, tool_id, "publish", reason, version)

    async def release(
        self,
        principal: Principal,
        tool_id: str,
        action: str,
        reason: str,
        version: int | None = None,
    ):
        if not principal.can_publish:
            raise HTTPException(403, "FORBIDDEN")
        async with self.db.begin() as conn:
            locked = await rows(
                conn,
                "SELECT active_version,lifecycle FROM tools WHERE id=:id FOR UPDATE",
                id=tool_id,
            )
            versions = await rows(
                conn,
                "SELECT version,contract,source FROM tool_versions WHERE tool_id=:id ORDER BY version DESC",
                id=tool_id,
            )
            if not locked or not versions:
                raise HTTPException(404, "TOOL_NOT_AVAILABLE")
            current = locked[0]
            latest = versions[0]
            if current["lifecycle"] == "archived" and action != "archive":
                raise HTTPException(409, "TOOL_ARCHIVED")
            active = None
            lifecycle = {
                "disable": "disabled",
                "archive": "archived",
                "review": "review",
            }.get(action, "published")
            if action in {"publish", "rollback"}:
                candidate = next(
                    (item for item in versions if item["version"] == version), None
                )
                if not candidate:
                    raise HTTPException(404, "TOOL_NOT_AVAILABLE")
                if candidate["contract"].get("blocked") or (
                    latest["source"] is not None
                    and version != latest["version"]
                    and (
                        latest["contract"].get("blocked")
                        or candidate["contract"] != latest["contract"]
                    )
                ):
                    raise HTTPException(409, "VERSION_INCOMPATIBLE")
                quarantines = await rows(
                    conn,
                    "SELECT version FROM publications WHERE tool_id=:id AND action='quarantine' ORDER BY id DESC LIMIT 1",
                    id=tool_id,
                )
                if (
                    quarantines
                    and version is not None
                    and version <= quarantines[0]["version"]
                ):
                    raise HTTPException(409, "VERSION_INCOMPATIBLE")
                if (
                    current["lifecycle"] == "quarantined"
                    and version != latest["version"]
                ):
                    raise HTTPException(409, "VERSION_INCOMPATIBLE")
                if action == "rollback":
                    published = await rows(
                        conn,
                        "SELECT version FROM publications WHERE tool_id=:id AND version=:version AND action IN ('publish','rollback') LIMIT 1",
                        id=tool_id,
                        version=version,
                    )
                    if not published or (
                        current["active_version"]
                        and version >= current["active_version"]
                    ):
                        raise HTTPException(409, "INVALID_ROLLBACK")
                active = version
            elif action == "review":
                if current["active_version"] is not None:
                    raise HTTPException(409, "INVALID_TRANSITION")
                if current["lifecycle"] == "quarantined":
                    lifecycle = "quarantined"
            version = version or current["active_version"] or latest["version"]
            if (
                active == current["active_version"]
                and lifecycle == current["lifecycle"]
                and action != "review"
            ):
                return {"id": tool_id, "version": version, "status": lifecycle}
            await execute(
                conn,
                "UPDATE tools SET active_version=:version,lifecycle=:lifecycle WHERE id=:id",
                id=tool_id,
                version=active,
                lifecycle=lifecycle,
            )
            await execute(
                conn,
                """
                INSERT INTO publications(tool_id,version,actor,reason,action)
                VALUES (:id,:version,:actor,:reason,:action)
            """,
                id=tool_id,
                version=version,
                actor=principal.id,
                reason=reason,
                action=action,
            )
        return {"id": tool_id, "version": version, "status": lifecycle}

    async def versions(self, tool_id):
        async with self.db.connect() as conn:
            versions = await rows(
                conn,
                "SELECT version,contract,source FROM tool_versions WHERE tool_id=:id ORDER BY version DESC",
                id=tool_id,
            )
            history = await rows(
                conn,
                "SELECT version,actor,reason,action,created_at FROM publications WHERE tool_id=:id ORDER BY id DESC LIMIT 50",
                id=tool_id,
            )
        if not versions:
            raise HTTPException(404, "TOOL_NOT_AVAILABLE")
        return {"versions": versions, "history": history}
