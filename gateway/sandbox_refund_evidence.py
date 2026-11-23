"""Read-only evidence for the disposable synthetic refund ledger."""

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
        return sha256(self.binding_preimage()).digest()

    def binding_preimage(self) -> bytes:
        return canonical_json(
            [
                "synthetic-refund.v1",
                self.owner.model_dump(mode="json"),
                self.scope.model_dump(mode="json"),
                self.request.model_dump(mode="json"),
                self.sealed_digest.hex(),
            ]
        )


async def read_refund_evidence(
    db: AsyncEngine, key: bytes, intent: RefundIntent
) -> SyntheticOutcome | None:
    operation_id = intent.owner.execution_id
    async with db.begin() as conn:
        await execute(conn, "SET TRANSACTION READ ONLY")
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
        update={"attestation": digest(key, synthetic_evidence_bytes(outcome), "sha256")}
    )
