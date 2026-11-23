"""Shared evidence-lookup orchestration for disposable protected adapters."""

from collections.abc import Awaitable, Callable
from typing import Any, Generic, Protocol, TypeVar
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from gateway.execution_approval import ExecutionApprovals
from gateway.execution_contracts import (
    ExecutionOwner,
    ExecutionState,
    ProtectedIntentScope,
    ProtectedTool,
    SyntheticEvidenceContract,
    SyntheticOutcome,
)
from gateway.execution_store import CommandStoreError
from gateway.execution_worker_store import ExecutionWorkerStore


class LookupIntent(Protocol):
    @property
    def owner(self) -> ExecutionOwner: ...

    @property
    def binding(self) -> bytes: ...


Intent = TypeVar("Intent", bound=LookupIntent)
InputIntent = TypeVar("InputIntent", bound=LookupIntent, contravariant=True)


class LookupLedger(Protocol[InputIntent]):
    async def invoke(self, intent: InputIntent) -> None: ...
    async def evidence(self, intent: InputIntent) -> SyntheticOutcome | None: ...


class SyntheticLookupFlow(Generic[Intent]):
    def __init__(
        self,
        approvals: ExecutionApprovals,
        ownership: ExecutionWorkerStore,
        ledger: LookupLedger[Intent],
        *,
        tool: ProtectedTool,
        build_intent: Callable[
            [ExecutionOwner, ProtectedIntentScope, dict[str, Any], bytes], Intent
        ],
        evidence_contract: SyntheticEvidenceContract | None = None,
    ):
        self._approvals = approvals
        self._ownership = ownership
        self._ledger = ledger
        self._tool = tool
        self._build_intent = build_intent
        self._evidence_contract = evidence_contract

    async def run_once(
        self,
        worker_id: UUID,
        *,
        after_consumption: Callable[[], Awaitable[None]] | None = None,
    ) -> ExecutionState | None:
        lease = await self._ownership.claim(worker_id, tool_id=self._tool)
        if lease is None:
            return None
        renewed = await self._ownership.renew(lease.owner)
        if renewed is None:
            return None
        owner = renewed.owner
        intent: Intent | None = None

        async def authorize(conn: AsyncConnection) -> bytes:
            nonlocal intent
            (
                scope,
                arguments,
                sealed_digest,
            ) = await self._approvals.verify_dispatch_intent(
                conn, owner.execution_id, self._tool
            )
            try:
                current = self._build_intent(owner, scope, arguments, sealed_digest)
            except ValidationError:
                raise CommandStoreError("EXECUTION_INTENT_MISMATCH") from None
            if intent is not None and intent != current:
                raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
            intent = current
            return current.binding

        marker = await self._ownership.mark_synthetic_send(
            owner, admit=authorize, evidence_contract=self._evidence_contract
        )
        if marker is None or intent is None:
            return None
        # An ambiguous marker or consumption commit unwinds the call. Rereading
        # either record cannot grant permission to invoke.
        if not await self._ownership.consume_synthetic_send(
            marker, admit=authorize, evidence_contract=self._evidence_contract
        ):
            return None
        if after_consumption is not None:
            await after_consumption()
        try:
            await self._ledger.invoke(intent)
        except (SQLAlchemyError, OSError, TimeoutError):
            # An exception can follow a committed effect. Read the independent
            # ledger before deciding whether the outcome is known.
            pass
        return await self._resolve(intent, invoked_here=True)

    async def recover(self, owner: ExecutionOwner) -> ExecutionState | None:
        """Recover by reading the sealed intent and independent evidence."""
        observed = await self._ownership.read_synthetic_attempt(owner)
        if (
            not observed
            or not observed["marked"]
            or observed["current_generation"] != owner.generation
        ):
            return None
        try:
            (
                scope,
                arguments,
                sealed_digest,
            ) = await self._approvals.read_sealed_dispatch_intent(
                owner.execution_id, self._tool
            )
        except CommandStoreError as error:
            if str(error) != "EXECUTION_PAYLOAD_UNAVAILABLE":
                raise
            if await self._ownership.classify_synthetic_unknown(owner, expired=True):
                return ExecutionState.UNKNOWN
            current = await self._ownership.read_synthetic_attempt(owner)
            if (
                current
                and current["current_generation"] == owner.generation
                and current["state"] == ExecutionState.UNKNOWN
            ):
                return ExecutionState.UNKNOWN
            raise
        return await self._resolve(
            self._build_intent(owner, scope, arguments, sealed_digest)
        )

    async def _resolve(
        self, intent: Intent, *, invoked_here: bool = False
    ) -> ExecutionState | None:
        owner = intent.owner
        try:
            outcome = await self._ledger.evidence(intent)
        except (SQLAlchemyError, OSError, TimeoutError):
            outcome = None
        if (
            outcome is not None
            and outcome.owner == owner
            and outcome.operation_id == owner.execution_id
            and outcome.intent_binding == intent.binding
            and outcome.evidence_contract == self._evidence_contract
        ):
            try:
                await self._ownership.finalize_synthetic(outcome)
            except CommandStoreError as error:
                if str(error) != "EXECUTION_OWNERSHIP_UNAVAILABLE":
                    raise
            observed = await self._ownership.read_synthetic_attempt(owner, outcome)
            if observed and observed["receipt_matches"]:
                return observed["state"]
            # Keep a live marker available for evidence-only finalization
            # after a rollback. Expired ownership must still fail its fence.
            invoked_here = False
        if invoked_here:
            await self._ownership.classify_synthetic_unknown(owner)
        await self._ownership.classify_synthetic_unknown(owner, expired=True)
        observed = await self._ownership.read_synthetic_attempt(owner)
        if observed and observed["current_generation"] == owner.generation:
            if observed["state"] in {ExecutionState.SUCCEEDED, ExecutionState.FAILED}:
                raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
            return observed["state"]
        return None
