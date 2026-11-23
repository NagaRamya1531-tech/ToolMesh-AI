"""Persistence contracts for protected commands. No dispatch or cryptography."""

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

ProtectedTool = Literal["payments.issue_refund", "it.restart_service"]
SyntheticEvidenceContract = Literal["it-restart-ledger.v1"]
NonEmpty = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(ge=1)]
Generation = Annotated[int, Field(ge=0)]
Digest = Annotated[bytes, Field(min_length=32, max_length=32)]
NonEmptyBytes = Annotated[bytes, Field(min_length=1)]


class ExecutionState(StrEnum):
    RECEIVED = "RECEIVED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    READY = "READY"
    CLAIMED = "CLAIMED"
    DISPATCHING = "DISPATCHING"
    UNKNOWN = "UNKNOWN"
    DENIED = "DENIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    @property
    def public_status(self) -> str:
        if self in {self.READY, self.CLAIMED, self.DISPATCHING}:
            return "RUNNING"
        return self.value


class ApprovalState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class DispatchMode(StrEnum):
    LEGACY = "LEGACY"
    PAUSED = "PAUSED"
    DURABLE = "DURABLE"


class AttemptKind(StrEnum):
    SEND = "SEND"
    VERIFY = "VERIFY"


class AttemptOutcome(StrEnum):
    NOT_SENT = "NOT_SENT"
    UNKNOWN = "UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ConfirmedOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


_TRANSITIONS = {
    ExecutionState.RECEIVED: {
        ExecutionState.PENDING_APPROVAL,
        ExecutionState.DENIED,
        ExecutionState.FAILED,
    },
    ExecutionState.PENDING_APPROVAL: {
        ExecutionState.READY,
        ExecutionState.REJECTED,
        ExecutionState.EXPIRED,
    },
    ExecutionState.READY: {ExecutionState.CLAIMED, ExecutionState.FAILED},
    ExecutionState.CLAIMED: {
        ExecutionState.READY,
        ExecutionState.DENIED,
        ExecutionState.FAILED,
        ExecutionState.DISPATCHING,
    },
    ExecutionState.DISPATCHING: {
        ExecutionState.SUCCEEDED,
        ExecutionState.FAILED,
        ExecutionState.UNKNOWN,
    },
    # Slice A permits verification only once a send is uncertain.
    ExecutionState.UNKNOWN: {ExecutionState.SUCCEEDED, ExecutionState.FAILED},
}


def allows_transition(current: ExecutionState, target: ExecutionState) -> bool:
    """Check a lifecycle edge. Ownership, authorization and evidence are separate."""
    return target in _TRANSITIONS.get(current, set())


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionOwner(Record):
    execution_id: UUID
    worker_id: UUID
    generation: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]


class ExecutionLease(Record):
    owner: ExecutionOwner
    expires_at: AwareDatetime


class RefundVerification(Record):
    lease: ExecutionLease
    sender: ExecutionOwner
    intent_binding: Digest = Field(repr=False)

    @model_validator(mode="after")
    def validate_generations(self) -> Self:
        if (
            self.lease.owner.execution_id != self.sender.execution_id
            or self.lease.owner.generation <= self.sender.generation
        ):
            raise ValueError(
                "verification requires a later generation of the original execution"
            )
        return self


class SyntheticSendMarker(Record):
    owner: ExecutionOwner
    marked_at: AwareDatetime
    admission_revision: PositiveInt


class SyntheticOutcome(Record):
    owner: ExecutionOwner
    operation_id: UUID
    result: Literal["EFFECT_COMMITTED", "DEFINITIVE_NO_EFFECT"]
    attestation: Digest = Field(repr=False)
    intent_binding: Digest | None = Field(default=None, repr=False)
    evidence_contract: SyntheticEvidenceContract | None = None
    effect_version: Annotated[int, Field(strict=True, gt=0)] | None = None


class PreSendTransition(Record):
    execution_id: UUID
    generation: Generation
    state: Literal[ExecutionState.READY, ExecutionState.FAILED]


class SealedPayload(Record):
    """Opaque storage envelope. Authenticity is checked by the future crypto layer."""

    ciphertext: NonEmptyBytes
    wrapped_data_key: NonEmptyBytes
    nonce: Annotated[bytes, Field(min_length=12, max_length=12)]
    key_version: PositiveInt


class ProtectedIntentScope(Record):
    workspace_id: NonEmpty
    requester: NonEmpty
    customer_id: NonEmpty
    tool_id: ProtectedTool


class ExecutionCommand(ProtectedIntentScope):
    execution_id: UUID
    tool_version: PositiveInt
    contract_digest: Digest
    state: ExecutionState
    payload: SealedPayload | None = None
    scheduled_at: AwareDatetime
    dispatch_not_after: AwareDatetime | None = None
    lease_owner: UUID | None = None
    lease_expires_at: AwareDatetime | None = None
    generation: Generation = 0
    verification_only: bool = False
    next_check_at: AwareDatetime | None = None

    @property
    def operation_id(self) -> UUID:
        """The execution and approval UUID is also the domain operation ID."""
        return self.execution_id

    @model_validator(mode="after")
    def validate_state_shape(self) -> Self:
        sendable = self.state in {
            ExecutionState.READY,
            ExecutionState.CLAIMED,
            ExecutionState.DISPATCHING,
        }
        owned = self.lease_owner is not None
        if owned != (self.lease_expires_at is not None):
            raise ValueError("lease owner and expiry must be supplied together")
        if owned and (
            self.generation == 0
            or self.state
            not in {
                ExecutionState.CLAIMED,
                ExecutionState.DISPATCHING,
                ExecutionState.UNKNOWN,
            }
        ):
            raise ValueError(
                "only claimed, dispatching or unknown work can own a lease"
            )
        if (
            self.state in {ExecutionState.CLAIMED, ExecutionState.DISPATCHING}
            and not owned
        ):
            raise ValueError("claimed or dispatching work requires a lease")
        if sendable and (
            self.verification_only
            or self.payload is None
            or self.dispatch_not_after is None
        ):
            raise ValueError("sendable work needs a payload and dispatch deadline")
        if self.state is ExecutionState.PENDING_APPROVAL and self.payload is None:
            raise ValueError("pending approval requires an immutable payload")
        if self.verification_only and self.state not in {
            ExecutionState.UNKNOWN,
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
        }:
            raise ValueError("verification-only commands must be unknown or resolved")
        if self.next_check_at is not None and self.state is not ExecutionState.UNKNOWN:
            raise ValueError("only unknown work can schedule verification")
        return self


class ExecutionAttempt(Record):
    execution_id: UUID
    generation: PositiveInt
    worker_id: UUID
    kind: AttemptKind
    claimed_at: AwareDatetime
    send_marked_at: AwareDatetime | None = None
    admission_revision: PositiveInt | None = None
    finished_at: AwareDatetime | None = None
    outcome: AttemptOutcome | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        marked = self.send_marked_at is not None
        if marked != (self.admission_revision is not None):
            raise ValueError("send marker and admission revision must be paired")
        if marked and self.kind is not AttemptKind.SEND:
            raise ValueError("verification cannot mark a send")
        if (self.finished_at is None) != (self.outcome is None):
            raise ValueError("finished attempt requires an outcome")
        if (
            self.kind is AttemptKind.SEND
            and self.outcome is not None
            and marked == (self.outcome is AttemptOutcome.NOT_SENT)
        ):
            raise ValueError("send outcome must agree with its marker")
        if self.kind is AttemptKind.VERIFY and self.outcome is AttemptOutcome.NOT_SENT:
            raise ValueError("verification does not report a send outcome")
        return self


class ExecutionReceipt(Record):
    execution_id: UUID
    generation: PositiveInt
    outcome: ConfirmedOutcome
    evidence_source: Literal["RESPONSE", "LOOKUP"]
    external_reference: NonEmpty
    evidence_digest: Digest
    recorded_at: AwareDatetime
    payload: SealedPayload | None = None


class IdempotencyReservation(ProtectedIntentScope):
    execution_id: UUID
    tool_version: PositiveInt
    key_digest: Digest
    intent_digest: Digest
    digest_key_version: PositiveInt
    canonicalizer_version: PositiveInt


class LegacyKeyAlias(Record):
    requester: NonEmpty
    tool_id: ProtectedTool
    key_digest: Digest
    execution_id: UUID


class AuthorizationRevisionGuard(Record):
    singleton: Literal[True] = True
    revision: PositiveInt = 1


class ApprovalEntitlement(Record):
    principal: NonEmpty
    customer_id: NonEmpty
    tool_id: ProtectedTool
    enabled: bool = True


class DispatchGate(Record):
    singleton: Literal[True] = True
    mode: DispatchMode = DispatchMode.LEGACY
