import asyncio
import json
from uuid import UUID, uuid4

from fastapi import HTTPException
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy.ext.asyncio import AsyncEngine

from control_plane.sources import Target, fetch, simple_object
from shared.contracts import TOOLS, tool_contract
from shared.database import execute, rows


def differences(old: dict, new: dict) -> dict:
    previous, current = old.get("properties", {}), new.get("properties", {})
    required, new_required = set(old.get("required", [])), set(new.get("required", []))
    added, removed = set(current) - set(previous), set(previous) - set(current)
    changed = {
        key for key in set(current) & set(previous) if current[key] != previous[key]
    }
    return {
        "added": sorted(added),
        "removed": sorted(removed),
        "changed": sorted(changed),
        "required_added": sorted(new_required - required),
        "required_removed": sorted(required - new_required),
        "breaking": bool(removed or changed or new_required - required),
    }


class Crawler:
    def __init__(self, db: AsyncEngine, targets: dict[str, Target]):
        self.db = db
        self.targets = targets

    async def register(self, actor: str, target: str, name: str):
        if target not in self.targets:
            raise HTTPException(422, "TARGET_NOT_ALLOWED")
        async with self.db.begin() as conn:
            added = await rows(
                conn,
                """
                INSERT INTO services(id,name,protocol,owner) VALUES (:id,:name,:protocol,:owner)
                ON CONFLICT DO NOTHING RETURNING id
            """,
                id=target,
                name=name,
                protocol=self.targets[target].protocol,
                owner=actor,
            )
        if not added:
            raise HTTPException(409, "SERVICE_ALREADY_REGISTERED")
        return added[0]

    async def rename(self, service: str, name: str):
        async with self.db.begin() as conn:
            updated = await rows(
                conn,
                "UPDATE services SET name=:name WHERE id=:id RETURNING id,name",
                id=service,
                name=name,
            )
        if not updated:
            raise HTTPException(404, "SERVICE_NOT_AVAILABLE")
        return updated[0]

    async def test_connection(self, service: str):
        async with self.db.connect() as conn:
            known = await rows(conn, "SELECT id FROM services WHERE id=:id", id=service)
        if not known or service not in self.targets:
            raise HTTPException(404, "SERVICE_NOT_AVAILABLE")
        error = None
        try:
            async with asyncio.timeout(8):
                await fetch(service, self.targets[service])
            health = "healthy"
        except Exception:
            health, error = "unreachable", "CONNECTION_FAILED"
        async with self.db.begin() as conn:
            values = await rows(
                conn,
                """
                UPDATE services SET health=:health,health_error=:error,checked_at=now()
                WHERE id=:id RETURNING health,health_error,checked_at
            """,
                health=health,
                error=error,
                id=service,
            )
        return values[0]

    async def enqueue(self, service: str, actor: str):
        async with self.db.begin() as conn:
            found = await rows(conn, "SELECT id FROM services WHERE id=:id", id=service)
            if not found or service not in self.targets:
                raise HTTPException(404, "SERVICE_NOT_AVAILABLE")
            queued = await rows(
                conn,
                """
                INSERT INTO crawl_jobs(id,service_id,actor) VALUES (:id,:service,:actor)
                ON CONFLICT DO NOTHING RETURNING id,status
            """,
                id=uuid4(),
                service=service,
                actor=actor,
            )
            if not queued:
                raise HTTPException(409, "CRAWL_ALREADY_ACTIVE")
        return queued[0]

    async def jobs(self, id: UUID | None = None):
        async with self.db.connect() as conn:
            return await rows(
                conn,
                """
                SELECT * FROM crawl_jobs WHERE cast(:id AS uuid) IS NULL OR id=:id
                ORDER BY requested_at DESC LIMIT 100
            """,
                id=id,
            )

    async def claim(self):
        async with self.db.begin() as conn:
            found = await rows(
                conn,
                """
                UPDATE crawl_jobs SET status='RUNNING',started_at=clock_timestamp()
                WHERE id=(SELECT id FROM crawl_jobs WHERE status='QUEUED'
                    OR (status='RUNNING' AND started_at<now()-interval '30 seconds')
                    ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                RETURNING id,service_id,started_at
            """,
            )
        return found[0] if found else None

    async def crawl(self, job: dict):
        service = job["service_id"]
        try:
            async with asyncio.timeout(10):
                collected, skipped = await fetch(service, self.targets[service])
        except Exception as exc:
            reason = (
                str(exc)
                if isinstance(exc, ValueError)
                and str(exc).isupper()
                and len(str(exc)) < 64
                else "METADATA_UNAVAILABLE"
            )
            async with self.db.begin() as conn:
                await execute(
                    conn,
                    "UPDATE crawl_jobs SET status='FAILED',error=:reason,finished_at=now() WHERE id=:id AND started_at=:lease",
                    id=job["id"],
                    reason=reason,
                    lease=job["started_at"],
                )
                await execute(
                    conn,
                    "UPDATE services SET health='unreachable',checked_at=now(),health_error=:reason WHERE id=:id",
                    id=service,
                    reason=reason,
                )
            return
        async with self.db.begin() as conn:
            current = await rows(
                conn,
                "SELECT id FROM crawl_jobs WHERE id=:id AND started_at=:lease FOR UPDATE",
                id=job["id"],
                lease=job["started_at"],
            )
            if not current:
                return
            changes = [{"status": "unsupported", "name": name} for name in skipped]
            active = await rows(
                conn,
                "SELECT id FROM tools WHERE service_id=:service AND lifecycle<>'archived'",
                service=service,
            )
            for row in active:
                if row["id"] not in collected:
                    await self.quarantine(conn, row["id"])
                    changes.append(
                        {
                            "status": "quarantined",
                            "tool_id": row["id"],
                            "reason": "OPERATION_MISSING",
                        }
                    )
            for identifier, source in collected.items():
                try:
                    changes.append(await self.stage(conn, service, identifier, source))
                except (ValueError, SchemaError):
                    await self.quarantine(conn, identifier)
                    changes.append(
                        {
                            "status": "unsupported",
                            "tool_id": identifier,
                            "reason": "UNSUPPORTED_SCHEMA",
                        }
                    )
            await execute(
                conn,
                """
                UPDATE crawl_jobs SET status='SUCCEEDED',finished_at=now(),changes=CAST(:changes AS jsonb) WHERE id=:id
            """,
                id=job["id"],
                changes=json.dumps(changes),
            )
            await execute(
                conn,
                "UPDATE services SET health=:health,checked_at=now(),health_error=NULL WHERE id=:service",
                service=service,
                health="degraded"
                if any(c["status"] in {"unsupported", "quarantined"} for c in changes)
                else "healthy",
            )

    @staticmethod
    async def quarantine(conn, identifier, replacement: bool = False):
        locked = await rows(
            conn,
            """
            SELECT active_version,lifecycle,(SELECT max(version) FROM tool_versions WHERE tool_id=:id) AS newest
            FROM tools WHERE id=:id FOR UPDATE
        """,
            id=identifier,
        )
        if not locked or locked[0]["lifecycle"] == "archived":
            return
        item = locked[0]
        blocked = item["active_version"] if replacement else item["newest"]
        if blocked is None:
            return
        prior = await rows(
            conn,
            "SELECT version FROM publications WHERE tool_id=:id AND action='quarantine' ORDER BY id DESC LIMIT 1",
            id=identifier,
        )
        if (
            item["lifecycle"] == "quarantined"
            and prior
            and prior[0]["version"] == blocked
        ):
            return
        await execute(
            conn,
            "UPDATE tools SET active_version=NULL,lifecycle='quarantined' WHERE id=:id",
            id=identifier,
        )
        await execute(
            conn,
            "INSERT INTO publications(tool_id,version,actor,reason,action) VALUES (:id,:version,'system','Crawler detected incompatible or missing source metadata','quarantine')",
            id=identifier,
            version=blocked,
        )

    async def stage(self, conn, service, identifier, source):
        if identifier == "inventory.get_inventory":
            for schema in source.values():
                simple_object(schema)
                Draft202012Validator.check_schema(schema)
            if "customer_id" not in source["input_schema"].get("required", []):
                raise ValueError("UNSUPPORTED_SCHEMA")
            contract = {
                **source,
                "risk": "read",
                "description": "Read a persisted sandbox inventory item.",
            }
        elif identifier in TOOLS:
            contract = tool_contract(identifier)
        else:
            raise ValueError("UNSUPPORTED_OPERATION")
        await execute(
            conn,
            "INSERT INTO tools(id,service_id) VALUES (:id,:service) ON CONFLICT DO NOTHING",
            id=identifier,
            service=service,
        )
        state = (
            await rows(
                conn,
                "SELECT active_version,lifecycle FROM tools WHERE id=:id FOR UPDATE",
                id=identifier,
            )
        )[0]
        versions = await rows(
            conn,
            "SELECT version,source,contract FROM tool_versions WHERE tool_id=:id ORDER BY version DESC",
            id=identifier,
        )
        prior = await rows(
            conn,
            "SELECT version FROM publications WHERE tool_id=:id AND action='quarantine' ORDER BY id DESC LIMIT 1",
            id=identifier,
        )
        if state["lifecycle"] == "archived":
            return {"status": "archived", "tool_id": identifier}
        if (
            versions
            and versions[0]["source"] == source
            and (not prior or versions[0]["version"] > prior[0]["version"])
        ):
            return {
                "status": "unchanged",
                "tool_id": identifier,
                "version": versions[0]["version"],
            }
        version = versions[0]["version"] + 1 if versions else 1
        baseline = next(
            (item for item in versions if item["version"] == state["active_version"]),
            versions[0] if versions else None,
        )
        before = (
            baseline["source"]
            if baseline and baseline["source"]
            else baseline["contract"]
            if baseline
            else None
        )
        diffs = {}
        if before:
            for key in ("input_schema", "output_schema"):
                # Legacy models can contain named components. Compare published wire metadata on later crawls.
                if (
                    baseline is not None
                    and baseline["source"] is None
                    and identifier in TOOLS
                ):
                    continue
                diffs[key] = differences(before[key], source[key])
        out = diffs.get("output_schema", {})
        breaking = any(change["breaking"] for change in diffs.values()) or bool(
            out.get("required_removed") or out.get("added")
        )
        if identifier in TOOLS:
            wire = source["input_schema"]
            known = contract["input_schema"]
            breaking = breaking or not (
                set(wire.get("required", [])) <= set(known.get("required", []))
                and set(known.get("properties", {})) <= set(wire.get("properties", {}))
            )
        if identifier in TOOLS and breaking:
            # Existing adapters use reviewed static serializers. Require an adapter update before activation.
            contract = {**contract, "blocked": True}
        await execute(
            conn,
            """
            INSERT INTO tool_versions(tool_id,version,contract,source)
            VALUES (:id,:version,CAST(:contract AS jsonb),CAST(:source AS jsonb))
        """,
            id=identifier,
            version=version,
            contract=json.dumps(contract),
            source=json.dumps(source),
        )
        if breaking and state["active_version"] is not None:
            await self.quarantine(conn, identifier, replacement=True)
        return {
            "status": "quarantined"
            if breaking and state["active_version"] is not None
            else "draft",
            "tool_id": identifier,
            "version": version,
            "diff": diffs,
        }

    async def worker(self):
        while True:
            try:
                job = await self.claim()
                if job:
                    await self.crawl(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                import logging

                logging.getLogger(__name__).warning("Crawler pass interrupted")
            await asyncio.sleep(0.75)
