from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from gateway.execution_contracts import (
    ApprovalState,
    DispatchGate,
    DispatchMode,
    ExecutionAttempt,
    ExecutionCommand,
    ExecutionState,
    allows_transition,
)


def test_status_projection_keeps_approval_and_uncertainty_distinct():
    assert ApprovalState.APPROVED.value == "APPROVED"
    for state in (
        ExecutionState.READY,
        ExecutionState.CLAIMED,
        ExecutionState.DISPATCHING,
    ):
        assert state.public_status == "RUNNING"
    assert ExecutionState.UNKNOWN.public_status == "UNKNOWN"
    assert DispatchGate().mode is DispatchMode.LEGACY


def test_slice_a_transition_graph_prevents_blind_replay_and_terminal_reopening():
    assert allows_transition(ExecutionState.PENDING_APPROVAL, ExecutionState.READY)
    assert allows_transition(ExecutionState.CLAIMED, ExecutionState.READY)
    assert allows_transition(ExecutionState.DISPATCHING, ExecutionState.UNKNOWN)
    assert allows_transition(ExecutionState.UNKNOWN, ExecutionState.SUCCEEDED)
    assert not allows_transition(ExecutionState.UNKNOWN, ExecutionState.READY)
    assert not allows_transition(ExecutionState.SUCCEEDED, ExecutionState.READY)


def test_command_validates_scope_and_lease_shape():
    command = {
        "execution_id": uuid4(),
        "workspace_id": "default",
        "requester": "finance-agent",
        "customer_id": "C-102",
        "tool_id": "payments.issue_refund",
        "tool_version": 1,
        "contract_digest": bytes(32),
        "state": "UNKNOWN",
        "scheduled_at": datetime.now(UTC),
        "verification_only": True,
    }
    assert (
        ExecutionCommand.model_validate(command).operation_id == command["execution_id"]
    )
    for invalid in (
        {"tool_id": "support.create_ticket"},
        {"lease_owner": uuid4()},
        {"state": "READY"},
        {"state": "PENDING_APPROVAL", "verification_only": False},
    ):
        with pytest.raises(ValidationError):
            ExecutionCommand.model_validate(command | invalid)


def test_verification_attempt_cannot_mark_send():
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="verification cannot mark a send"):
        ExecutionAttempt(
            execution_id=uuid4(),
            generation=1,
            worker_id=uuid4(),
            kind="VERIFY",
            claimed_at=now,
            send_marked_at=now,
            admission_revision=1,
        )
