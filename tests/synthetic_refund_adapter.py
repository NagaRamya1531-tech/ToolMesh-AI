"""Refund-specific downstream simulator for disposable PostgreSQL."""

from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.execution_contracts import SyntheticOutcome
from gateway.sandbox_refund_evidence import (
    RefundIntent as RefundIntent,
)
from gateway.sandbox_refund_evidence import (
    RefundRequest as RefundRequest,
)
from gateway.sandbox_refund_evidence import read_refund_evidence
from shared.database import execute, rows


class RefundLedger:
    def __init__(self, db: AsyncEngine, key: bytes):
        self._db = db
        self._key = key

    async def initialize(self) -> None:
        async with self._db.begin() as conn:
            await execute(conn, "CREATE SCHEMA synthetic_refund")
            await execute(
                conn,
                """
                CREATE TABLE synthetic_refund.captures (
                    workspace_id text, customer_id text, transaction_id text, currency text,
                    available_minor bigint NOT NULL CHECK (available_minor >= 0),
                    PRIMARY KEY (workspace_id, customer_id, transaction_id, currency)
                )
                """,
            )
            await execute(
                conn,
                "CREATE TABLE synthetic_refund.arrivals (operation_id uuid NOT NULL)",
            )
            await execute(
                conn,
                """
                CREATE TABLE synthetic_refund.outcomes (
                    operation_id uuid PRIMARY KEY,
                    binding bytea NOT NULL CHECK (octet_length(binding)=32),
                    refunded_minor bigint NOT NULL CHECK (refunded_minor >= 0)
                )
                """,
            )

    async def close(self) -> None:
        async with self._db.begin() as conn:
            await execute(conn, "DROP SCHEMA synthetic_refund CASCADE")

    async def invoke(self, intent: RefundIntent) -> None:
        operation_id = intent.owner.execution_id
        async with self._db.begin() as conn:
            await execute(
                conn,
                "INSERT INTO synthetic_refund.arrivals VALUES (:id)",
                id=operation_id,
            )
        async with self._db.begin() as conn:
            # Reserve the stable ID before touching a capture. A conflicting
            # request never alters the original row or commits a second effect.
            reserved = await rows(
                conn,
                """
                INSERT INTO synthetic_refund.outcomes VALUES (:id,:binding,0)
                ON CONFLICT (operation_id) DO NOTHING RETURNING operation_id
                """,
                id=operation_id,
                binding=intent.binding,
            )
            if reserved:
                updated = await rows(
                    conn,
                    """
                    UPDATE synthetic_refund.captures
                    SET available_minor=available_minor-:amount
                    WHERE workspace_id=:workspace AND customer_id=:customer
                      AND transaction_id=:transaction AND currency=:currency
                      AND available_minor >= :amount
                    RETURNING available_minor
                    """,
                    workspace=intent.scope.workspace_id,
                    customer=intent.scope.customer_id,
                    transaction=intent.request.transaction_id,
                    currency=intent.request.currency,
                    amount=intent.request.amount_minor,
                )
                if updated:
                    await execute(
                        conn,
                        "UPDATE synthetic_refund.outcomes SET refunded_minor=:amount WHERE operation_id=:id",
                        amount=intent.request.amount_minor,
                        id=operation_id,
                    )

    async def evidence(self, intent: RefundIntent) -> SyntheticOutcome | None:
        return await read_refund_evidence(self._db, self._key, intent)
