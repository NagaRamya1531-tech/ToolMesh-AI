"""Disposable adapter with independent arrival and effect records."""

import asyncio
import json
import sys
from datetime import timedelta
from hmac import digest

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from gateway.execution_contracts import SyntheticOutcome, SyntheticSendMarker
from gateway.execution_worker_store import (
    ExecutionWorkerStore,
    synthetic_evidence_bytes,
)
from shared.database import execute, rows


class SyntheticAdapter:
    def __init__(self, db: AsyncEngine, key: bytes):
        self._db = db
        self._key = key

    async def initialize(self):
        async with self._db.begin() as conn:
            await execute(
                conn,
                """
                CREATE FUNCTION public.protect_synthetic_dispatch_evidence() RETURNS trigger
                LANGUAGE plpgsql SET search_path = pg_catalog AS $$
                BEGIN
                    IF TG_OP = 'TRUNCATE' THEN
                        IF EXISTS (SELECT 1 FROM public.audit_events
                                   WHERE (action IN ('SYNTHETIC_INVOCATION_CONSUMED', 'SYNTHETIC_SEND_MARKED')
                               OR starts_with(action, 'SYNTHETIC_SEND_MARKED:'))) THEN
                            RAISE EXCEPTION USING ERRCODE = '42501',
                                MESSAGE = 'synthetic consumption evidence is protected';
                        END IF;
                        RETURN NULL;
                    END IF;
                    IF TG_OP = 'INSERT' THEN
                        IF (NEW.action IN ('SYNTHETIC_INVOCATION_CONSUMED', 'SYNTHETIC_SEND_MARKED')
                               OR starts_with(NEW.action, 'SYNTHETIC_SEND_MARKED:'))
                           AND current_user <> pg_get_userbyid((
                               SELECT relowner FROM pg_class WHERE oid = TG_RELID
                           )) THEN
                            RAISE EXCEPTION USING ERRCODE = '42501',
                                MESSAGE = 'synthetic consumption action is reserved';
                        END IF;
                        RETURN NEW;
                    END IF;
                    IF (OLD.action IN ('SYNTHETIC_INVOCATION_CONSUMED', 'SYNTHETIC_SEND_MARKED')
                               OR starts_with(OLD.action, 'SYNTHETIC_SEND_MARKED:')) THEN
                        RAISE EXCEPTION USING ERRCODE = '42501',
                            MESSAGE = 'synthetic consumption evidence is protected';
                    END IF;
                    IF TG_OP = 'UPDATE' THEN
                        IF (NEW.action IN ('SYNTHETIC_INVOCATION_CONSUMED', 'SYNTHETIC_SEND_MARKED')
                               OR starts_with(NEW.action, 'SYNTHETIC_SEND_MARKED:')) THEN
                            RAISE EXCEPTION USING ERRCODE = '42501',
                                MESSAGE = 'synthetic consumption evidence is protected';
                        END IF;
                        RETURN NEW;
                    END IF;
                    RETURN OLD;
                END;
                $$
                """,
            )
            await execute(
                conn,
                """
                CREATE TRIGGER protect_synthetic_consumption_rows
                BEFORE INSERT OR UPDATE OR DELETE ON public.audit_events FOR EACH ROW
                EXECUTE FUNCTION public.protect_synthetic_dispatch_evidence()
                """,
            )
            await execute(
                conn,
                """
                CREATE TRIGGER protect_synthetic_consumption_truncate
                BEFORE TRUNCATE ON public.audit_events FOR EACH STATEMENT
                EXECUTE FUNCTION public.protect_synthetic_dispatch_evidence()
                """,
            )
            for trigger in (
                "protect_synthetic_consumption_rows",
                "protect_synthetic_consumption_truncate",
            ):
                await execute(
                    conn,
                    f"ALTER TABLE public.audit_events ENABLE ALWAYS TRIGGER {trigger}",
                )
            await execute(
                conn,
                """
                CREATE TABLE synthetic_arrivals (operation_id uuid NOT NULL);
                """,
            )
            await execute(
                conn,
                """
                CREATE TABLE synthetic_outcomes (
                    operation_id uuid PRIMARY KEY, effect boolean NOT NULL
                )
                """,
            )

    async def close(self):
        async with self._db.begin() as conn:
            await execute(
                conn,
                "DROP TRIGGER protect_synthetic_consumption_rows ON public.audit_events",
            )
            await execute(
                conn,
                "DROP TRIGGER protect_synthetic_consumption_truncate ON public.audit_events",
            )
            await execute(
                conn, "DROP FUNCTION public.protect_synthetic_dispatch_evidence()"
            )
            await execute(conn, "DROP TABLE synthetic_arrivals, synthetic_outcomes")

    async def invoke(
        self, marker: SyntheticSendMarker, *, effect: bool
    ) -> SyntheticOutcome:
        operation_id = marker.owner.execution_id
        async with self._db.begin() as conn:
            await execute(
                conn, "INSERT INTO synthetic_arrivals VALUES (:id)", id=operation_id
            )
        async with self._db.begin() as conn:
            await execute(
                conn,
                """
                INSERT INTO synthetic_outcomes VALUES (:id,:effect)
                ON CONFLICT (operation_id) DO NOTHING
                """,
                id=operation_id,
                effect=effect,
            )
            [record] = await rows(
                conn,
                "SELECT effect FROM synthetic_outcomes WHERE operation_id=:id",
                id=operation_id,
            )
        evidence = SyntheticOutcome(
            owner=marker.owner,
            operation_id=operation_id,
            result="EFFECT_COMMITTED" if record["effect"] else "DEFINITIVE_NO_EFFECT",
            attestation=bytes(32),
        )
        return evidence.model_copy(
            update={
                "attestation": digest(
                    self._key, synthetic_evidence_bytes(evidence), "sha256"
                )
            }
        )

    async def counts(self, operation_id):
        async with self._db.connect() as conn:
            [counts] = await rows(
                conn,
                """
                SELECT (SELECT count(*) FROM synthetic_arrivals WHERE operation_id=:id) AS arrivals,
                       (SELECT count(*) FROM synthetic_outcomes WHERE operation_id=:id AND effect) AS effects
                """,
                id=operation_id,
            )
        return counts["arrivals"], counts["effects"]


class SyntheticHarness:
    def __init__(self, store: ExecutionWorkerStore, adapter: SyntheticAdapter):
        self._store = store
        self._adapter = adapter

    async def send_if_current(
        self, marker: SyntheticSendMarker, *, effect: bool, before_invoke=None
    ):
        if not await self._store.consume_synthetic_send(marker):
            return None
        if before_invoke is not None:
            await before_invoke()
        return await self._adapter.invoke(marker, effect=effect)


async def _invoke_in_child():
    """Test subprocess entry point. The parent owns fixture setup and teardown."""
    config = json.loads(await asyncio.to_thread(sys.stdin.readline))
    key = bytes.fromhex(config["key"])
    db = create_async_engine(config["database_url"], hide_parameters=True)
    try:
        harness = SyntheticHarness(
            ExecutionWorkerStore(db, timedelta(seconds=30), synthetic_key=key),
            SyntheticAdapter(db, key),
        )
        marker = SyntheticSendMarker.model_validate(config["marker"])
        print("ready", flush=True)
        if await asyncio.to_thread(sys.stdin.readline) != "start\n":
            raise RuntimeError("missing subprocess start signal")
        outcome = await harness.send_if_current(marker, effect=True)
        print("permitted" if outcome is not None else "denied", flush=True)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(_invoke_in_child())
