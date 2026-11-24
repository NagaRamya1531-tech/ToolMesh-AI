"""Shared evidence-only reconciliation for disposable PostgreSQL."""

from asyncio import timeout
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from gateway.execution_approval import ExecutionApprovals
from gateway.execution_contracts import (
    ExecutionOwner,
    ExecutionState,
    ProtectedIntentScope,
    RefundDeferralReason,
    RefundVerification,
    SyntheticOutcome,
)
from gateway.execution_store import CommandStoreError
from gateway.execution_worker_store import ExecutionWorkerStore
from gateway.sandbox_refund_evidence import RefundIntent, RefundRequest
from gateway.sandbox_refund_store import SandboxRefundStore, SandboxRestartStore
from gateway.sandbox_restart_evidence import RestartIntent, restart_intent

TOOL = "payments.issue_refund"


class VerificationIntent(Protocol):
    @property
    def binding(self) -> bytes: ...
    def binding_preimage(self) -> bytes: ...


class VerificationReconciler[Intent: VerificationIntent]:
    def __init__(
        self,
        approvals: ExecutionApprovals,
        store: ExecutionWorkerStore | SandboxRefundStore,
        lookup: Callable[[Intent], Awaitable[SyntheticOutcome | None]],
        build_intent: Callable[
            [ExecutionOwner, ProtectedIntentScope, dict, bytes], Intent
        ],
        *,
        defer_for: timedelta = timedelta(seconds=5),
    ):
        if defer_for <= timedelta(0):
            raise ValueError("verification deferral must be positive")
        self._approvals = approvals
        self._store = store
        self._lookup = lookup
        self._build_intent = build_intent
        self._defer_for = defer_for

    async def _finish(
        self,
        claim: RefundVerification,
        *,
        reason: RefundDeferralReason,
        evidence: SyntheticOutcome | None = None,
        preimage: bytes | None = None,
    ) -> ExecutionState | None:
        if isinstance(self._store, SandboxRefundStore):
            return await self._store.finish_synthetic_refund_verification(
                claim,
                evidence=evidence,
                binding_preimage=preimage,
                deferral_reason=reason,
            )
        return await self._store.finish_synthetic_refund_verification(
            claim,
            evidence=evidence,
            binding_preimage=preimage,
            defer_for=self._defer_for,
            deferral_reason=reason,
        )

    async def run_once(
        self,
        execution_id: UUID,
        verifier: UUID,
        *,
        stopping: Callable[[], bool] = lambda: False,
        mutation: Callable[[str], AbstractContextManager[None]] = lambda _: (
            nullcontext()
        ),
    ) -> ExecutionState | None:
        if stopping():
            return None
        with mutation("verify_claim"):
            claim = await self._store.claim_synthetic_refund_verification(
                execution_id, verifier
            )
        if claim is None or stopping():
            return None
        evidence = None
        preimage = None
        reason: RefundDeferralReason = "NO_EVIDENCE"
        try:
            async with timeout(claim.lookup_timeout_ms / 1000):
                if isinstance(self._store, SandboxRefundStore):
                    scope, args, sealed_digest = await self._store.read_intent(claim)
                else:
                    (
                        scope,
                        args,
                        sealed_digest,
                    ) = await self._approvals.read_sealed_dispatch_intent(
                        execution_id, TOOL
                    )
                intent = self._build_intent(claim.sender, scope, args, sealed_digest)
                if intent.binding != claim.intent_binding:
                    raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
                evidence = await self._lookup(intent)
                if evidence is not None:
                    preimage = intent.binding_preimage()
        except TimeoutError:
            reason = "TIMEOUT"
        except (SQLAlchemyError, OSError):
            reason = "UNAVAILABLE"
        except ValidationError:
            reason = "MISMATCH"
        except CommandStoreError as error:
            if str(error) in {
                "EXECUTION_PAYLOAD_UNAVAILABLE",
                "APPROVAL_STORE_UNAVAILABLE",
            }:
                reason = "UNAVAILABLE"
            elif str(error) in {
                "APPROVAL_NOT_AVAILABLE",
                "EXECUTION_INTENT_MISMATCH",
                "SYNTHETIC_EVIDENCE_MISMATCH",
            }:
                reason = "MISMATCH"
            else:
                raise
        if stopping():
            return None
        with mutation("verify_finish"):
            try:
                return await self._finish(
                    claim, evidence=evidence, preimage=preimage, reason=reason
                )
            except CommandStoreError as error:
                if str(error) == "SYNTHETIC_EVIDENCE_MISMATCH":
                    if stopping():
                        return None
                    return await self._finish(claim, reason="MISMATCH")
                if (
                    str(error) == "EXECUTION_OWNERSHIP_UNAVAILABLE"
                    and evidence is not None
                ):
                    committed = (
                        await self._store.read_completed_synthetic_refund_verification(
                            claim, evidence
                        )
                    )
                    if committed is not None:
                        return committed
                raise


class RefundReconciler(VerificationReconciler[RefundIntent]):
    def __init__(
        self,
        approvals: ExecutionApprovals,
        store: ExecutionWorkerStore | SandboxRefundStore,
        lookup: Callable[[RefundIntent], Awaitable[SyntheticOutcome | None]],
        *,
        defer_for: timedelta = timedelta(seconds=5),
    ):
        super().__init__(
            approvals,
            store,
            lookup,
            lambda owner, scope, args, digest: RefundIntent(
                owner, scope, RefundRequest.model_validate(args), digest
            ),
            defer_for=defer_for,
        )


class RestartReconciler(VerificationReconciler[RestartIntent]):
    def __init__(
        self,
        approvals: ExecutionApprovals,
        store: SandboxRestartStore,
        lookup: Callable[[RestartIntent], Awaitable[SyntheticOutcome | None]],
    ):
        super().__init__(approvals, store, lookup, restart_intent)
