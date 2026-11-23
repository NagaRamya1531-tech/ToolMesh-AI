"""Opt-in refund orchestration used only by the disposable sandbox."""

from typing import Final

from synthetic_lookup_flow import SyntheticLookupFlow
from synthetic_refund_adapter import RefundIntent, RefundLedger, RefundRequest

from gateway.execution_approval import ExecutionApprovals
from gateway.execution_contracts import ExecutionOwner, ProtectedIntentScope
from gateway.execution_worker_store import ExecutionWorkerStore

TOOL: Final = "payments.issue_refund"


def _refund_intent(
    owner: ExecutionOwner,
    scope: ProtectedIntentScope,
    arguments: dict,
    sealed_digest: bytes,
) -> RefundIntent:
    return RefundIntent(
        owner, scope, RefundRequest.model_validate(arguments), sealed_digest
    )


class RefundFlow(SyntheticLookupFlow[RefundIntent]):
    def __init__(
        self,
        approvals: ExecutionApprovals,
        ownership: ExecutionWorkerStore,
        ledger: RefundLedger,
    ):
        super().__init__(
            approvals,
            ownership,
            ledger,
            tool=TOOL,
            build_intent=_refund_intent,
        )
