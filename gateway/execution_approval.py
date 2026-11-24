"""Transactional protected approvals. This module performs no dispatch."""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from typing import Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from gateway.access import POLICY, object_allowed, scope_params
from gateway.execution_contracts import (
    ApprovalState,
    ExecutionState,
    ProtectedIntentScope,
    SealedPayload,
)
from gateway.execution_crypto import (
    CommandDigests,
    PayloadContext,
    PayloadProtection,
    PayloadUnavailable,
    canonical_json,
)
from gateway.execution_store import (
    CANONICALIZER_VERSION,
    CommandStoreError,
    canonical_arguments,
    lock_durable_coordination,
    protected_intent_digest,
)
from gateway.identity import resolve_principal
from shared.database import execute, rows


@dataclass(frozen=True)
class ApprovalResult:
    execution_id: UUID
    approval_status: ApprovalState
    execution_state: ExecutionState
    dispatch_not_after: datetime | None
    replayed: bool = False
    error_code: str | None = None


@dataclass(frozen=True)
class LockedApproval:
    execution: dict
    approval: dict
    command: dict
    reservation: dict
    catalog: dict


class ExecutionApprovals:
    """An internal service. Callers must authenticate the decision actor."""

    def __init__(
        self,
        db: AsyncEngine,
        payloads: PayloadProtection,
        digests: CommandDigests,
        dispatch_window: timedelta,
    ):
        if not db.sync_engine.hide_parameters or db.echo:
            raise ValueError(
                "approval store requires hidden SQL parameters and SQL echo off"
            )
        if dispatch_window <= timedelta(0):
            raise ValueError("dispatch window must be positive")
        self._db = db
        self._payloads = payloads
        self._digests = digests
        self._dispatch_window = dispatch_window

    @asynccontextmanager
    async def _transaction(self):
        try:
            async with self._db.connect() as conn:
                await conn.execution_options(isolation_level="READ COMMITTED")
                async with conn.begin():
                    await execute(conn, "SET LOCAL lock_timeout = '2s'")
                    revision = await lock_durable_coordination(conn)
                    yield conn, revision
        except PayloadUnavailable:
            raise CommandStoreError("EXECUTION_PAYLOAD_UNAVAILABLE") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            # The caller can retry the exact decision after an ambiguous commit.
            raise CommandStoreError("APPROVAL_STORE_UNAVAILABLE") from None

    async def decide(
        self,
        execution_id: UUID,
        actor: str,
        decision: Literal["APPROVED", "REJECTED"],
        reason: str,
        *,
        workspace_id: str | None = None,
    ) -> ApprovalResult:
        if decision not in ("APPROVED", "REJECTED") or not isinstance(reason, str):
            raise CommandStoreError("INVALID_DECISION")
        reason = reason.strip()
        if not 1 <= len(reason) <= 240:
            raise CommandStoreError("INVALID_DECISION")
        try:
            reason_bytes = canonical_json(reason)
        except ValueError:
            raise CommandStoreError("INVALID_DECISION") from None
        async with self._transaction() as (conn, revision):
            principal = await resolve_principal(conn, actor)
            if workspace_id is not None and (
                principal is None
                or principal.kind != "human"
                or not await rows(
                    conn,
                    """
                    SELECT 1 FROM execution_commands
                    WHERE execution_id=:id AND workspace_id=:workspace AND customer_id=:customer
                      AND requester<>:actor
                    """,
                    id=execution_id,
                    workspace=workspace_id,
                    customer=principal.customer,
                    actor=actor,
                )
            ):
                raise CommandStoreError("APPROVAL_NOT_AVAILABLE")
            item = await self._lock(conn, execution_id)
            approval = item.approval
            status = ApprovalState(approval["status"])
            if (
                principal is None
                or principal.kind != "human"
                or principal.customer != approval["customer_id"]
                or principal.id == approval["requester"]
            ):
                raise CommandStoreError("APPROVAL_NOT_AVAILABLE")

            # Current entitlement controls new decisions. The original actor
            # can recover a committed decision, subject to the checks below.
            owns_decision = (
                status in (ApprovalState.APPROVED, ApprovalState.REJECTED)
                and approval["decided_by"] == principal.id
            )
            if not owns_decision and not await rows(
                conn,
                """
                SELECT 1 FROM execution_approval_entitlements
                WHERE principal=:actor AND customer_id=:customer AND tool_id=:tool AND enabled
                """,
                actor=actor,
                customer=approval["customer_id"],
                tool=approval["tool_id"],
            ):
                raise CommandStoreError("APPROVAL_NOT_AVAILABLE")

            scope, arguments = await self._verify_intent(conn, execution_id, item)
            reason_digest = self._digests.intent(
                canonical_json(
                    ["approval-decision.v1", str(execution_id), actor, decision, reason]
                )
            )
            if status is not ApprovalState.PENDING:
                if status is ApprovalState.EXPIRED:
                    return self._result(execution_id, item, replayed=True)
                if status.value != decision or approval["decided_by"] != actor:
                    raise CommandStoreError("APPROVAL_ALREADY_DECIDED")
                prior = await rows(
                    conn,
                    "SELECT * FROM execution_approval_decisions WHERE execution_id=:id",
                    id=execution_id,
                )
                if not prior or not compare_digest(
                    prior[0]["reason_digest"], reason_digest
                ):
                    raise CommandStoreError("APPROVAL_ALREADY_DECIDED")
                if (
                    self._open(
                        prior[0],
                        PayloadContext("approval-decision", execution_id, scope),
                    )
                    != reason_bytes
                ):
                    raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
                return self._result(execution_id, item, replayed=True)

            [clock] = await rows(conn, "SELECT clock_timestamp() AS current_time")
            now = clock["current_time"]
            if approval["expires_at"] <= now:
                return await self._expire(conn, execution_id, item, now)
            if decision == "APPROVED":
                await self._authorize_requester(conn, item, scope, arguments)
            sealed = self._payloads.seal(
                reason_bytes, PayloadContext("approval-decision", execution_id, scope)
            )
            # Check again after validation and encryption before recording the
            # decision. The database clock is evaluated after row-lock waits.
            [clock] = await rows(conn, "SELECT clock_timestamp() AS current_time")
            now = clock["current_time"]
            if approval["expires_at"] <= now:
                return await self._expire(conn, execution_id, item, now)
            deadline = now + self._dispatch_window if decision == "APPROVED" else None
            state = (
                ExecutionState.READY
                if decision == "APPROVED"
                else ExecutionState.REJECTED
            )
            await execute(
                conn,
                """
                INSERT INTO execution_approval_decisions (
                    execution_id,authorization_revision,reason_digest,ciphertext,
                    wrapped_data_key,nonce,key_version
                ) VALUES (:id,:revision,:digest,:ciphertext,:wrapped_data_key,:nonce,:key_version)
                """,
                id=execution_id,
                revision=revision,
                digest=reason_digest,
                **sealed.model_dump(),
            )
            return await self._transition(
                conn,
                execution_id,
                item,
                ApprovalState(decision),
                state,
                now,
                actor=actor,
                deadline=deadline,
            )

    async def _lock_dispatch_approval(
        self, conn: AsyncConnection, execution_id: UUID, tool_id: str
    ) -> LockedApproval:
        item = await self._lock(conn, execution_id)
        if (
            item.approval["status"] != ApprovalState.APPROVED
            or item.command["tool_id"] != tool_id
            or item.command["verification_only"]
        ):
            raise CommandStoreError("APPROVAL_NOT_AVAILABLE")
        return item

    async def read_sealed_dispatch_intent(
        self, execution_id: UUID, tool_id: str
    ) -> tuple[ProtectedIntentScope, dict, bytes]:
        """Read an already-approved intent for evidence lookup only."""
        async with self._transaction() as (conn, _):
            item = await self._lock_dispatch_approval(conn, execution_id, tool_id)
            scope, arguments = await self._verify_intent(conn, execution_id, item)
            return scope, arguments, item.reservation["intent_digest"]

    async def verify_dispatch_intent(
        self, conn: AsyncConnection, execution_id: UUID, tool_id: str
    ) -> tuple[ProtectedIntentScope, dict, bytes]:
        """Verify an existing approval inside the caller's coordination transaction."""
        item = await self._lock_dispatch_approval(conn, execution_id, tool_id)
        approval = item.approval
        actor = await resolve_principal(conn, approval["decided_by"])
        if (
            actor is None
            or actor.kind != "human"
            or actor.id == approval["requester"]
            or actor.customer != approval["customer_id"]
            or not await rows(
                conn,
                """
                SELECT 1 FROM execution_approval_entitlements
                WHERE principal=:actor AND customer_id=:customer AND tool_id=:tool AND enabled
                """,
                actor=actor.id,
                customer=approval["customer_id"],
                tool=tool_id,
            )
        ):
            raise CommandStoreError("APPROVER_AUTHORIZATION_CHANGED")

        decisions = await rows(
            conn,
            "SELECT * FROM execution_approval_decisions WHERE execution_id=:id",
            id=execution_id,
        )
        if not decisions:
            raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
        try:
            scope, arguments = await self._verify_intent(conn, execution_id, item)
            reason = self._open(
                decisions[0], PayloadContext("approval-decision", execution_id, scope)
            )
        except PayloadUnavailable:
            raise CommandStoreError("EXECUTION_PAYLOAD_UNAVAILABLE") from None
        expected = self._digests.intent(
            canonical_json(
                [
                    "approval-decision.v1",
                    str(execution_id),
                    actor.id,
                    "APPROVED",
                    json.loads(reason),
                ]
            )
        )
        if not compare_digest(decisions[0]["reason_digest"], expected):
            raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
        await self._authorize_requester(conn, item, scope, arguments)
        return scope, arguments, item.reservation["intent_digest"]

    async def expire(self, execution_id: UUID) -> ApprovalResult:
        """Close an expired pending approval without needing to decrypt arguments."""
        async with self._transaction() as (conn, _):
            item = await self._lock(conn, execution_id)
            [clock] = await rows(conn, "SELECT clock_timestamp() AS current_time")
            if item.approval["status"] != "PENDING" or (
                item.approval["expires_at"] > clock["current_time"]
            ):
                return self._result(execution_id, item, replayed=True)
            return await self._expire(conn, execution_id, item, clock["current_time"])

    @staticmethod
    async def _lock(conn: AsyncConnection, execution_id: UUID) -> LockedApproval:
        target = await rows(
            conn,
            "SELECT tool_id FROM execution_commands WHERE execution_id=:id",
            id=execution_id,
        )
        if not target:
            raise CommandStoreError("APPROVAL_NOT_AVAILABLE")
        catalog = await rows(
            conn,
            "SELECT id, active_version, lifecycle FROM tools WHERE id=:tool FOR SHARE",
            tool=target[0]["tool_id"],
        )
        reservation = await rows(
            conn,
            "SELECT * FROM execution_request_keys WHERE execution_id=:id FOR SHARE",
            id=execution_id,
        )
        execution = await rows(
            conn,
            "SELECT id,principal,tool_id,version,status,error_code FROM executions WHERE id=:id FOR UPDATE",
            id=execution_id,
        )
        approval = await rows(
            conn, "SELECT * FROM approvals WHERE id=:id FOR UPDATE", id=execution_id
        )
        command = await rows(
            conn,
            "SELECT * FROM execution_commands WHERE execution_id=:id FOR UPDATE",
            id=execution_id,
        )
        if not all((catalog, reservation, execution, approval, command)):
            raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
        return LockedApproval(
            execution[0], approval[0], command[0], reservation[0], catalog[0]
        )

    def _open(self, row: dict, context: PayloadContext) -> bytes:
        try:
            payload = SealedPayload.model_validate(
                {column: row[column] for column in SealedPayload.model_fields}
            )
        except (KeyError, ValidationError):
            raise CommandStoreError("EXECUTION_INTENT_MISMATCH") from None
        return self._payloads.open(payload, context)

    async def _verify_intent(
        self, conn: AsyncConnection, execution_id: UUID, item: LockedApproval
    ) -> tuple[ProtectedIntentScope, dict]:
        c = item.command
        contract = await rows(
            conn,
            "SELECT contract FROM tool_versions WHERE tool_id=:tool AND version=:version",
            tool=c["tool_id"],
            version=c["tool_version"],
        )
        return self.verify_sealed_intent(
            execution_id, item, contract[0]["contract"] if contract else None
        )

    def verify_sealed_intent(
        self, execution_id: UUID, item: LockedApproval, contract: dict | None
    ) -> tuple[ProtectedIntentScope, dict]:
        """Authenticate a sealed snapshot from a scoped database reader."""
        c, k, a, e = item.command, item.reservation, item.approval, item.execution
        scope = ProtectedIntentScope(
            workspace_id=c["workspace_id"],
            requester=c["requester"],
            customer_id=c["customer_id"],
            tool_id=c["tool_id"],
        )
        version = c["tool_version"]
        if (
            any(
                k[key] != c[key]
                for key in (
                    "workspace_id",
                    "requester",
                    "customer_id",
                    "tool_id",
                    "tool_version",
                )
            )
            or (e["principal"], e["tool_id"], e["version"])
            != (scope.requester, scope.tool_id, version)
            or item.catalog["id"] != scope.tool_id
            or k["digest_key_version"] != self._digests.version
            or k["canonicalizer_version"] != CANONICALIZER_VERSION
            or a["arguments"] != {}
            or a["request_key"] != f"m9-sealed:{k['key_digest'].hex()}"
            or not compare_digest(
                a["intent_hash"].encode(), k["intent_digest"].hex().encode()
            )
        ):
            raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
        if (
            not contract
            or contract.get("risk") != "approval"
            or not compare_digest(
                sha256(canonical_json(contract)).digest(),
                c["contract_digest"],
            )
        ):
            raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
        plaintext = self._open(
            c, PayloadContext("command", execution_id, scope, CANONICALIZER_VERSION)
        )
        try:
            arguments = json.loads(plaintext)
            if (
                not isinstance(arguments, dict)
                or arguments.get("customer_id") != scope.customer_id
            ):
                raise ValueError
            canonical = canonical_arguments(contract, arguments)
        except (ValueError, CommandStoreError):
            raise CommandStoreError("EXECUTION_INTENT_MISMATCH") from None
        if not compare_digest(
            protected_intent_digest(
                self._digests, scope, version, c["contract_digest"], canonical
            ),
            k["intent_digest"],
        ):
            raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
        return scope, arguments

    @staticmethod
    async def _authorize_requester(
        conn: AsyncConnection,
        item: LockedApproval,
        scope: ProtectedIntentScope,
        arguments: dict,
    ) -> None:
        requester = await resolve_principal(conn, scope.requester)
        version = item.command["tool_version"]
        if (
            requester is None
            or not object_allowed(requester, arguments)
            or item.catalog["lifecycle"] != "published"
            or item.catalog["active_version"] != version
            or not await rows(
                conn,
                "SELECT 1 FROM tool_versions v JOIN tools t ON t.id=v.tool_id "
                "WHERE v.tool_id=:tool AND v.version=:version AND " + POLICY,
                tool=scope.tool_id,
                version=version,
                **scope_params(requester),
            )
        ):
            raise CommandStoreError("REQUESTER_AUTHORIZATION_CHANGED")

    async def _expire(
        self,
        conn: AsyncConnection,
        execution_id: UUID,
        item: LockedApproval,
        now: datetime,
    ) -> ApprovalResult:
        return await self._transition(
            conn, execution_id, item, ApprovalState.EXPIRED, ExecutionState.EXPIRED, now
        )

    @staticmethod
    async def _transition(
        conn: AsyncConnection,
        execution_id: UUID,
        item: LockedApproval,
        status: ApprovalState,
        state: ExecutionState,
        now: datetime,
        *,
        actor: str | None = None,
        deadline: datetime | None = None,
    ) -> ApprovalResult:
        if (
            item.approval["status"] != "PENDING"
            or item.command["state"] != "PENDING_APPROVAL"
            or item.execution["status"] != "PENDING_APPROVAL"
        ):
            raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
        await execute(
            conn,
            """
            UPDATE approvals SET status=:status, decided_by=:actor, decided_at=:decided_at,
                decision_reason=NULL, reconcile_after=NULL WHERE id=:id
            """,
            id=execution_id,
            status=status.value,
            actor=actor,
            decided_at=now if actor else None,
        )
        await execute(
            conn,
            """
            UPDATE executions SET status=:status, finished_at=:finished_at WHERE id=:id
            """,
            id=execution_id,
            status=state.public_status,
            finished_at=None if state is ExecutionState.READY else now,
        )
        await execute(
            conn,
            """
            UPDATE execution_commands SET state=:state, dispatch_not_after=:deadline,
                scheduled_at=:now WHERE execution_id=:id
            """,
            id=execution_id,
            state=state.value,
            deadline=deadline,
            now=now,
        )
        await execute(
            conn,
            "INSERT INTO audit_events (actor,action,resource_id) VALUES (:actor,:action,:id)",
            actor=actor or "system",
            action=f"DURABLE_APPROVAL_{status.value}",
            id=execution_id,
        )
        return ApprovalResult(execution_id, status, state, deadline)

    @staticmethod
    def _result(
        execution_id: UUID, item: LockedApproval, *, replayed: bool
    ) -> ApprovalResult:
        status = ApprovalState(item.approval["status"])
        state = ExecutionState(item.command["state"])
        expected = {
            ApprovalState.PENDING: {ExecutionState.PENDING_APPROVAL},
            ApprovalState.APPROVED: {
                ExecutionState.READY,
                ExecutionState.CLAIMED,
                ExecutionState.DISPATCHING,
                ExecutionState.UNKNOWN,
                ExecutionState.SUCCEEDED,
                ExecutionState.DENIED,
                ExecutionState.FAILED,
            },
            ApprovalState.EXPIRED: {ExecutionState.EXPIRED},
            ApprovalState.REJECTED: {ExecutionState.REJECTED},
        }
        if (
            state not in expected[status]
            or state.public_status != item.execution["status"]
        ):
            raise CommandStoreError("EXECUTION_INTENT_MISMATCH")
        error_code = None
        if state is ExecutionState.FAILED:
            recorded = item.execution["error_code"]
            error_code = (
                recorded
                if recorded
                in {
                    "DISPATCH_DEADLINE_EXCEEDED",
                    "SYNTHETIC_DEFINITIVE_NO_EFFECT",
                }
                else "EXECUTION_FAILED"
            )
        return ApprovalResult(
            execution_id,
            status,
            state,
            item.command["dispatch_not_after"],
            replayed,
            error_code,
        )
