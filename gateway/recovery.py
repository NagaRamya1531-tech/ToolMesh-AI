import asyncio
import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.telemetry import Telemetry
from shared.database import rows

logger = logging.getLogger(__name__)


async def reconcile_stale(db: AsyncEngine, telemetry: Telemetry) -> None:
    async with db.begin() as conn:
        pending = await rows(
            conn,
            """SELECT COALESCE(EXTRACT(EPOCH FROM (now()-min(requested_at))), 0) AS age
               FROM approvals WHERE status='PENDING' AND expires_at > now()""",
        )
        outcomes = await rows(
            conn,
            """
            WITH stale AS (
                SELECT id FROM executions e
                WHERE status IN ('RECEIVED', 'RUNNING')
                  AND started_at < now() - interval '5 minutes'
                  AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.id=e.id)
                ORDER BY started_at
                LIMIT 100 FOR UPDATE SKIP LOCKED
            )
            UPDATE executions e SET
                status=CASE WHEN e.status='RUNNING' AND e.dispatch_risk IS DISTINCT FROM 'read'
                    THEN 'UNKNOWN' ELSE 'FAILED' END,
                error_code=CASE WHEN e.status='RUNNING' AND e.dispatch_risk IS DISTINCT FROM 'read'
                    THEN 'UNKNOWN_OUTCOME' ELSE 'EXECUTION_INTERRUPTED' END,
                finished_at=now()
            FROM stale WHERE e.id=stale.id RETURNING e.status
            """,
        )
    telemetry.approval_age.set(float(pending[0]["age"]))
    for outcome in outcomes:
        telemetry.recovered.labels(status=outcome["status"]).inc()


async def recovery_worker(db: AsyncEngine, telemetry: Telemetry) -> None:
    while True:
        try:
            with telemetry.span("execution.reconcile_stale"):
                await reconcile_stale(db, telemetry)
        except (SQLAlchemyError, TimeoutError, OSError):
            logger.warning("Stale execution recovery pass failed")
        await asyncio.sleep(10)
