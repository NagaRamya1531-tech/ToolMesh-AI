"""Refund-specific downstream simulator for disposable PostgreSQL."""

from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest, digest

from pydantic import BaseModel, ConfigDict, PositiveInt
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.execution_contracts import (
    ExecutionOwner,
    ProtectedIntentScope,
    SyntheticOutcome,
)
from gateway.execution_crypto import canonical_json
from gateway.execution_worker_store import synthetic_evidence_bytes
from shared.database import execute, rows


class RefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    customer_id: str
    transaction_id: str
    currency: str
    amount_minor: PositiveInt


@dataclass(frozen=True)
class RefundIntent:
    owner: ExecutionOwner
    scope: ProtectedIntentScope
    request: RefundRequest
    sealed_digest: bytes

    @property
    def binding(self) -> bytes:
        return sha256(
            canonical_json(
                [
                    "synthetic-refund.v1",
                    self.owner.model_dump(mode="json"),
                    self.scope.model_dump(mode="json"),
                    self.request.model_dump(mode="json"),
                    self.sealed_digest.hex(),
                ]
            )
        ).digest()


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
        operation_id = intent.owner.execution_id
        async with self._db.connect() as conn:
            observed = await rows(
                conn,
                "SELECT binding, refunded_minor FROM synthetic_refund.outcomes WHERE operation_id=:id",
                id=operation_id,
            )
        if not observed:
            return None
        evidence = observed[0]
        if not compare_digest(evidence["binding"], intent.binding) or evidence[
            "refunded_minor"
        ] not in (0, intent.request.amount_minor):
            return None
        outcome = SyntheticOutcome(
            owner=intent.owner,
            operation_id=operation_id,
            result="EFFECT_COMMITTED"
            if evidence["refunded_minor"]
            else "DEFINITIVE_NO_EFFECT",
            intent_binding=intent.binding,
            attestation=bytes(32),
        )
        return outcome.model_copy(
            update={
                "attestation": digest(
                    self._key, synthetic_evidence_bytes(outcome), "sha256"
                )
            }
        )
