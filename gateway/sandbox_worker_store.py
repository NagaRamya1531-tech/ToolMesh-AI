"""Client for the restricted ownership interface installed in disposable PostgreSQL."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.execution_contracts import (
    ExecutionLease,
    ExecutionOwner,
    PreSendTransition,
)
from gateway.execution_store import CommandStoreError
from shared.database import execute, rows

SUPPORTED_REVISION = "0011"
_SQL_ERRORS = {
    "M9141": "WORKER_SCHEMA_UNSUPPORTED",
    "M9142": "EXECUTION_COORDINATION_UNAVAILABLE",
    "M9143": "DURABLE_ADMISSION_DISABLED",
    "M9144": "EXECUTION_OWNERSHIP_CONFLICT",
    "M9145": "WORKER_ISOLATION_UNSUPPORTED",
    "42501": "WORKER_ACCESS_DENIED",
}


class SandboxWorkerStore:
    def __init__(self, db: AsyncEngine, lease_duration: timedelta):
        self._db = db
        self._lease_ms = int(lease_duration / timedelta(milliseconds=1))
        if not db.sync_engine.hide_parameters or db.echo:
            raise ValueError("worker connections must hide SQL parameters")

    async def validate_schema(self) -> None:
        try:
            async with self._db.begin() as conn:
                await execute(
                    conn, "SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ ONLY"
                )
                version = await rows(
                    conn, "SELECT version_num FROM public.alembic_version"
                )
        except DBAPIError as error:
            code = getattr(error.orig, "sqlstate", None)
            if code in {"42P01", "42703", "42501"}:
                raise CommandStoreError("WORKER_SCHEMA_UNAVAILABLE") from None
            raise CommandStoreError("EXECUTION_OWNERSHIP_UNAVAILABLE") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise CommandStoreError("EXECUTION_OWNERSHIP_UNAVAILABLE") from None
        if version != [{"version_num": SUPPORTED_REVISION}]:
            raise CommandStoreError("WORKER_SCHEMA_UNSUPPORTED")

    async def _request(self, sql: str, **parameters: object) -> list[dict]:
        try:
            async with self._db.begin() as conn:
                await execute(conn, "SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                await execute(conn, "SET LOCAL lock_timeout = '2s'")
                await execute(conn, "SET LOCAL statement_timeout = '3s'")
                return await rows(conn, sql, **parameters)
        except DBAPIError as error:
            code = str(getattr(error.orig, "sqlstate", ""))
            raise CommandStoreError(
                _SQL_ERRORS.get(code, "EXECUTION_OWNERSHIP_UNAVAILABLE")
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise CommandStoreError("EXECUTION_OWNERSHIP_UNAVAILABLE") from None

    async def register(self) -> UUID:
        [identity] = await self._request(
            "SELECT m914_ownership.register_worker() AS worker"
        )
        return identity["worker"]

    async def _operate(
        self, operation: str, worker: UUID, owner: ExecutionOwner | None = None
    ) -> dict | None:
        result = await self._request(
            "SELECT * FROM m914_ownership.operate(:operation,:worker,:id,:generation,:lease_ms)",
            operation=operation,
            worker=worker,
            id=owner.execution_id if owner else None,
            generation=owner.generation if owner else None,
            lease_ms=self._lease_ms,
        )
        return result[0] if result else None

    async def claim(self, worker: UUID) -> ExecutionLease | None:
        record = await self._operate("claim", worker)
        if record is None:
            return None
        return ExecutionLease(
            owner=ExecutionOwner(
                execution_id=record["execution_id"],
                worker_id=worker,
                generation=record["generation"],
            ),
            expires_at=record["expires_at"],
        )

    async def renew(self, owner: ExecutionOwner) -> ExecutionLease | None:
        record = await self._operate("renew", owner.worker_id, owner)
        if record is None:
            return None
        return ExecutionLease(owner=owner, expires_at=record["expires_at"])

    async def release(self, owner: ExecutionOwner) -> PreSendTransition | None:
        record = await self._operate("release", owner.worker_id, owner)
        return (
            PreSendTransition.model_validate(record, extra="ignore") if record else None
        )

    async def recover_next(self, worker: UUID) -> PreSendTransition | None:
        record = await self._operate("recover", worker)
        return (
            PreSendTransition.model_validate(record, extra="ignore") if record else None
        )
