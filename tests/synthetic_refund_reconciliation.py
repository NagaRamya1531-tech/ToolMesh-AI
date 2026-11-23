"""Evidence-only refund reconciler for disposable PostgreSQL."""

from asyncio import timeout
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from synthetic_refund_adapter import RefundIntent, RefundRequest
from synthetic_refund_flow import TOOL

from gateway.execution_approval import ExecutionApprovals
from gateway.execution_contracts import ExecutionState, SyntheticOutcome
from gateway.execution_store import CommandStoreError
from gateway.execution_worker_store import ExecutionWorkerStore


class RefundReconciler:
    def __init__(
        self,
        approvals: ExecutionApprovals,
        store: ExecutionWorkerStore,
        lookup: Callable[[RefundIntent], Awaitable[SyntheticOutcome | None]],
        *,
        defer_for: timedelta = timedelta(seconds=5),
    ):
        if defer_for <= timedelta(0):
            raise ValueError("verification deferral must be positive")
        self._approvals = approvals
        self._store = store
        self._lookup = lookup
        self._defer_for = defer_for

    async def run_once(
        self, execution_id: UUID, verifier: UUID
    ) -> ExecutionState | None:
        claim = await self._store.claim_synthetic_refund_verification(
            execution_id, verifier
        )
        if claim is None:
            return None
        try:
            (
                scope,
                args,
                sealed_digest,
            ) = await self._approvals.read_sealed_dispatch_intent(execution_id, TOOL)
            intent = RefundIntent(
                claim.sender, scope, RefundRequest.model_validate(args), sealed_digest
            )
            if intent.binding != claim.intent_binding:
                raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
            async with timeout(3):
                evidence = await self._lookup(intent)
        except (SQLAlchemyError, OSError, TimeoutError):
            return await self._store.finish_synthetic_refund_verification(
                claim, defer_for=self._defer_for, deferral_reason="UNAVAILABLE"
            )
        except ValidationError:
            return await self._store.finish_synthetic_refund_verification(
                claim, defer_for=self._defer_for, deferral_reason="MISMATCH"
            )
        except CommandStoreError as error:
            if str(error) in {
                "EXECUTION_PAYLOAD_UNAVAILABLE",
                "APPROVAL_STORE_UNAVAILABLE",
            }:
                return await self._store.finish_synthetic_refund_verification(
                    claim, defer_for=self._defer_for, deferral_reason="UNAVAILABLE"
                )
            if str(error) in {
                "APPROVAL_NOT_AVAILABLE",
                "EXECUTION_INTENT_MISMATCH",
                "SYNTHETIC_EVIDENCE_MISMATCH",
            }:
                return await self._store.finish_synthetic_refund_verification(
                    claim, defer_for=self._defer_for, deferral_reason="MISMATCH"
                )
            raise
        try:
            return await self._store.finish_synthetic_refund_verification(
                claim, evidence=evidence, defer_for=self._defer_for
            )
        except CommandStoreError as error:
            if str(error) == "SYNTHETIC_EVIDENCE_MISMATCH":
                return await self._store.finish_synthetic_refund_verification(
                    claim, defer_for=self._defer_for, deferral_reason="MISMATCH"
                )
            if str(error) == "EXECUTION_OWNERSHIP_UNAVAILABLE" and evidence is not None:
                committed = (
                    await self._store.read_completed_synthetic_refund_verification(
                        claim, evidence
                    )
                )
                if committed is not None:
                    return committed
            raise
