"""Read-only evidence for the disposable synthetic restart ledger."""

from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest, digest
from typing import Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.execution_contracts import (
    ExecutionOwner,
    ProtectedIntentScope,
    SyntheticOutcome,
)
from gateway.execution_crypto import canonical_json
from gateway.execution_worker_store import synthetic_evidence_bytes
from shared.database import execute, rows

TOOL: Final = "it.restart_service"
CONTRACT: Final = "it-restart-ledger.v1"


class RestartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    customer_id: str
    service_id: str


@dataclass(frozen=True)
class RestartIntent:
    owner: ExecutionOwner
    scope: ProtectedIntentScope
    request: RestartRequest
    sealed_digest: bytes

    @property
    def binding(self) -> bytes:
        return sha256(self.binding_preimage()).digest()

    def binding_preimage(self) -> bytes:
        return canonical_json(
            [
                CONTRACT,
                self.owner.model_dump(mode="json"),
                self.scope.model_dump(mode="json"),
                self.request.model_dump(mode="json"),
                self.sealed_digest.hex(),
            ]
        )


async def read_restart_evidence(
    db: AsyncEngine, key: bytes, intent: RestartIntent
) -> SyntheticOutcome | None:
    operation_id = intent.owner.execution_id
    async with db.begin() as conn:
        await execute(conn, "SET TRANSACTION READ ONLY")
        observed = await rows(
            conn,
            """
            SELECT binding,workspace_id,customer_id,service_id,sender_worker,sender_generation,
                prior_boot_generation,boot_generation
            FROM synthetic_restart.outcomes WHERE operation_id=:id
            """,
            id=operation_id,
        )
    if not observed or not compare_digest(observed[0]["binding"], intent.binding):
        return None
    [record] = observed
    if (
        record["workspace_id"],
        record["customer_id"],
        record["service_id"],
        record["sender_worker"],
        record["sender_generation"],
    ) != (
        intent.scope.workspace_id,
        intent.scope.customer_id,
        intent.request.service_id,
        intent.owner.worker_id,
        intent.owner.generation,
    ) or intent.request.customer_id != intent.scope.customer_id:
        return None
    generation = record["boot_generation"]
    before = record["prior_boot_generation"]
    if (generation is None) != (before is None) or (
        before is not None and (before < 0 or generation != before + 1)
    ):
        return None
    outcome = SyntheticOutcome(
        owner=intent.owner,
        operation_id=operation_id,
        result="DEFINITIVE_NO_EFFECT" if generation is None else "EFFECT_COMMITTED",
        intent_binding=intent.binding,
        evidence_contract=CONTRACT,
        effect_version=generation,
        attestation=bytes(32),
    )
    return outcome.model_copy(
        update={"attestation": digest(key, synthetic_evidence_bytes(outcome), "sha256")}
    )


def restart_intent(
    owner: ExecutionOwner,
    scope: ProtectedIntentScope,
    arguments: dict,
    sealed_digest: bytes,
) -> RestartIntent:
    return RestartIntent(
        owner, scope, RestartRequest.model_validate(arguments), sealed_digest
    )
