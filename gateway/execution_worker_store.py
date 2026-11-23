"""PostgreSQL ownership for protected work before send admission."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from hmac import digest as hmac_digest
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from gateway.execution_contracts import (
    ExecutionLease,
    ExecutionOwner,
    ExecutionState,
    PreSendTransition,
    RefundDeferralReason,
    RefundVerification,
    SyntheticEvidenceContract,
    SyntheticOutcome,
    SyntheticSendMarker,
)
from gateway.execution_crypto import canonical_json
from gateway.execution_store import CommandStoreError, lock_durable_coordination
from shared.database import execute, rows

_CLAIM = """
    SELECT e.id FROM execution_commands c JOIN executions e ON e.id=c.execution_id
    WHERE c.state='READY' AND c.scheduled_at <= statement_timestamp()
      AND c.dispatch_not_after > statement_timestamp()
"""
_CLAIM_ORDER = " ORDER BY c.scheduled_at, e.id LIMIT 1 FOR UPDATE OF e SKIP LOCKED"
_RECOVER = """
    SELECT e.id FROM execution_commands c JOIN executions e ON e.id=c.execution_id
    WHERE (c.state='CLAIMED' AND c.lease_expires_at <= statement_timestamp())
       OR (c.state='READY' AND c.dispatch_not_after <= statement_timestamp())
    ORDER BY CASE WHEN c.state='CLAIMED' THEN c.lease_expires_at
                  ELSE c.dispatch_not_after END, e.id
    LIMIT 1 FOR UPDATE OF e SKIP LOCKED
"""
_LOCK_EXECUTION = "SELECT id FROM executions WHERE id=:id FOR UPDATE"


SyntheticAdmission = Callable[[AsyncConnection], Awaitable[bytes]]
_SYNTHETIC_MARKER_ACTION = "SYNTHETIC_SEND_MARKED"
_REFUND_MARKER_PREFIX = (
    f"{_SYNTHETIC_MARKER_ACTION}:payments.issue_refund:refund-ledger.v2:"
)


def _synthetic_tool(contract: SyntheticEvidenceContract | None) -> str:
    if contract is None:
        return "payments.issue_refund"
    if contract == "it-restart-ledger.v1":
        return "it.restart_service"
    raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")


def _synthetic_marker_action(
    binding: bytes | None, contract: SyntheticEvidenceContract | None
) -> str:
    if binding is None and contract is None:
        return _SYNTHETIC_MARKER_ACTION
    if binding is None or len(binding) != 32:
        raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
    tool = _synthetic_tool(contract)
    version = contract or "refund-ledger.v2"
    return f"{_SYNTHETIC_MARKER_ACTION}:{tool}:{version}:{binding.hex()}"


async def _admit_synthetic(
    conn: AsyncConnection, admit: SyntheticAdmission | None
) -> bytes | None:
    if admit is None:
        return None
    binding = await admit(conn)
    if not isinstance(binding, bytes) or len(binding) != 32:
        raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
    return binding


def synthetic_evidence_bytes(evidence: SyntheticOutcome) -> bytes:
    content = evidence.model_dump(
        mode="json",
        exclude={
            "attestation",
            "intent_binding",
            "evidence_contract",
            "effect_version",
        },
    )
    if evidence.evidence_contract is not None:
        version = evidence.effect_version
        if (
            evidence.intent_binding is None
            or (
                evidence.result == "EFFECT_COMMITTED"
                and (type(version) is not int or version <= 0)
            )
            or (evidence.result == "DEFINITIVE_NO_EFFECT" and version is not None)
        ):
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
        _synthetic_tool(evidence.evidence_contract)
        return canonical_json(
            [
                "synthetic-outcome.v3",
                evidence.evidence_contract,
                content,
                evidence.intent_binding.hex(),
                evidence.effect_version,
            ]
        )
    if evidence.effect_version is not None:
        raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
    if evidence.intent_binding is None:
        return canonical_json(["synthetic-outcome.v1", content])
    return canonical_json(
        ["synthetic-outcome.v2", content, evidence.intent_binding.hex()]
    )


class ExecutionWorkerStore:
    """Ownership-only store. All transactions hold both coordination locks."""

    def __init__(
        self,
        db: AsyncEngine,
        lease_duration: timedelta,
        *,
        synthetic_key: bytes | None = None,
    ):
        if not db.sync_engine.hide_parameters or db.echo:
            raise ValueError(
                "worker store requires hidden SQL parameters and SQL echo off"
            )
        if lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        self._db = db
        self._lease_duration = lease_duration
        if synthetic_key is not None and len(synthetic_key) != 32:
            raise ValueError("synthetic evidence key must contain 32 bytes")
        self._synthetic_key = synthetic_key

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[tuple[AsyncConnection, int]]:
        try:
            async with self._db.connect() as conn:
                await conn.execution_options(isolation_level="READ COMMITTED")
                async with conn.begin():
                    await execute(conn, "SET LOCAL lock_timeout = '2s'")
                    await execute(conn, "SET LOCAL statement_timeout = '3s'")
                    revision = await lock_durable_coordination(conn)
                    yield conn, revision
        except (SQLAlchemyError, OSError, TimeoutError):
            # A lost commit acknowledgment is ambiguous. This store never retries it.
            raise CommandStoreError("EXECUTION_OWNERSHIP_UNAVAILABLE") from None

    @staticmethod
    async def _lock_command(
        conn: AsyncConnection, execution_id: UUID, *, dispatching: bool = False
    ) -> dict:
        approval = await rows(
            conn,
            "SELECT status FROM approvals WHERE id=:id FOR UPDATE",
            id=execution_id,
        )
        commands = await rows(
            conn,
            """
            SELECT state, generation, scheduled_at, dispatch_not_after,
                   lease_owner, lease_expires_at
            FROM execution_commands WHERE execution_id=:id FOR UPDATE
            """,
            id=execution_id,
        )
        phases = (
            (ExecutionState.DISPATCHING,)
            if dispatching
            else (ExecutionState.READY, ExecutionState.CLAIMED)
        )
        if not commands or commands[0]["state"] not in phases:
            return {}
        if not approval or approval[0]["status"] != "APPROVED":
            raise CommandStoreError("EXECUTION_OWNERSHIP_CONFLICT")
        # Once a send is possible, this pre-send store must leave the command alone.
        if not dispatching and await rows(
            conn,
            """
            SELECT 1 FROM execution_attempts
            WHERE execution_id=:id AND send_marked_at IS NOT NULL
            """,
            id=execution_id,
        ):
            raise CommandStoreError("EXECUTION_OWNERSHIP_CONFLICT")
        return commands[0]

    @staticmethod
    async def _clock(conn: AsyncConnection) -> datetime:
        [clock] = await rows(conn, "SELECT clock_timestamp() AS current_time")
        return clock["current_time"]

    async def claim(
        self, worker_id: UUID, *, tool_id: str | None = None
    ) -> ExecutionLease | None:
        async with self._transaction() as (conn, _):
            if tool_id is None:
                candidates = await rows(conn, _CLAIM + _CLAIM_ORDER)
            else:
                candidates = await rows(
                    conn,
                    _CLAIM + " AND c.tool_id=:tool_id" + _CLAIM_ORDER,
                    tool_id=tool_id,
                )
            if not candidates:
                return None
            execution_id = candidates[0]["id"]
            command = await self._lock_command(conn, execution_id)
            now = await self._clock(conn)
            if (
                not command
                or command["state"] != ExecutionState.READY
                or command["lease_owner"] is not None
                or not command["scheduled_at"] <= now < command["dispatch_not_after"]
            ):
                return None
            expires_at = min(now + self._lease_duration, command["dispatch_not_after"])
            [claimed] = await rows(
                conn,
                """
                UPDATE execution_commands SET state='CLAIMED', generation=generation+1,
                    lease_owner=:worker, lease_expires_at=:expires
                WHERE execution_id=:id RETURNING generation
                """,
                id=execution_id,
                worker=worker_id,
                expires=expires_at,
            )
            owner = ExecutionOwner(
                execution_id=execution_id,
                worker_id=worker_id,
                generation=claimed["generation"],
            )
            await execute(
                conn,
                """
                INSERT INTO execution_attempts (execution_id, generation, worker_id, kind, claimed_at)
                VALUES (:execution_id, :generation, :worker_id, 'SEND', :now)
                """,
                now=now,
                **owner.model_dump(),
            )
            return ExecutionLease(owner=owner, expires_at=expires_at)

    async def _lock_owned(
        self, conn: AsyncConnection, owner: ExecutionOwner, *, dispatching: bool = False
    ) -> dict:
        if not await rows(conn, _LOCK_EXECUTION, id=owner.execution_id):
            return {}
        command = await self._lock_command(
            conn, owner.execution_id, dispatching=dispatching
        )
        phase = ExecutionState.DISPATCHING if dispatching else ExecutionState.CLAIMED
        if (
            command.get("state") != phase
            or command["lease_owner"] != owner.worker_id
            or command["generation"] != owner.generation
        ):
            return {}
        return command

    async def renew(self, owner: ExecutionOwner) -> ExecutionLease | None:
        async with self._transaction() as (conn, _):
            command = await self._lock_owned(conn, owner)
            if not command:
                return None
            now = await self._clock(conn)
            if now >= min(command["lease_expires_at"], command["dispatch_not_after"]):
                return None
            expires_at = min(
                max(now + self._lease_duration, command["lease_expires_at"]),
                command["dispatch_not_after"],
            )
            await execute(
                conn,
                "UPDATE execution_commands SET lease_expires_at=:expires WHERE execution_id=:id",
                expires=expires_at,
                id=owner.execution_id,
            )
            return ExecutionLease(owner=owner, expires_at=expires_at)

    async def release(self, owner: ExecutionOwner) -> PreSendTransition | None:
        async with self._transaction() as (conn, _):
            command = await self._lock_owned(conn, owner)
            if not command:
                return None
            now = await self._clock(conn)
            if now >= command["lease_expires_at"]:
                return None
            return await self._close(conn, owner.execution_id, command, now)

    async def recover_next(self) -> PreSendTransition | None:
        """Recover one expired claim or close a READY command past its deadline."""
        async with self._transaction() as (conn, _):
            candidates = await rows(conn, _RECOVER)
            if not candidates:
                return None
            execution_id = candidates[0]["id"]
            command = await self._lock_command(conn, execution_id)
            now = await self._clock(conn)
            if command.get("state") == ExecutionState.CLAIMED:
                if command["lease_expires_at"] > now:
                    return None
            elif (
                command.get("state") != ExecutionState.READY
                or command["dispatch_not_after"] > now
            ):
                return None
            return await self._close(conn, execution_id, command, now)

    @staticmethod
    async def _close(
        conn: AsyncConnection, execution_id: UUID, command: dict, now: datetime
    ) -> PreSendTransition:
        generation = command["generation"]
        if command["state"] == ExecutionState.CLAIMED:
            closed = await rows(
                conn,
                """
                UPDATE execution_attempts SET finished_at=:now, outcome='NOT_SENT'
                WHERE execution_id=:id AND generation=:generation AND worker_id=:worker
                  AND kind='SEND' AND send_marked_at IS NULL AND outcome IS NULL
                RETURNING generation
                """,
                now=now,
                id=execution_id,
                generation=generation,
                worker=command["lease_owner"],
            )
            if not closed:
                raise CommandStoreError("EXECUTION_OWNERSHIP_CONFLICT")
        target = (
            ExecutionState.FAILED
            if command["dispatch_not_after"] <= now
            else ExecutionState.READY
        )
        await execute(
            conn,
            """
            UPDATE execution_commands SET state=:state, lease_owner=NULL, lease_expires_at=NULL
            WHERE execution_id=:id
            """,
            state=target.value,
            id=execution_id,
        )
        if target == ExecutionState.FAILED:
            projected = await rows(
                conn,
                """
                UPDATE executions SET status='FAILED', error_code='DISPATCH_DEADLINE_EXCEEDED',
                    finished_at=:now WHERE id=:id AND status='RUNNING' RETURNING id
                """,
                now=now,
                id=execution_id,
            )
            if not projected:
                raise CommandStoreError("EXECUTION_OWNERSHIP_CONFLICT")
        return PreSendTransition(
            execution_id=execution_id, generation=generation, state=target
        )

    def _require_synthetic_sandbox(self) -> None:
        if self._synthetic_key is None:
            raise CommandStoreError("SYNTHETIC_EXECUTION_DISABLED")

    @staticmethod
    async def _synthetic_audit(
        conn: AsyncConnection, owner: ExecutionOwner, action: str
    ) -> None:
        await execute(
            conn,
            "INSERT INTO audit_events(actor, action, resource_id) VALUES (:actor, :action, :id)",
            actor=str(owner.worker_id),
            action=action,
            id=owner.execution_id,
        )

    async def mark_synthetic_send(
        self,
        owner: ExecutionOwner,
        *,
        admit: SyntheticAdmission | None = None,
        evidence_contract: SyntheticEvidenceContract | None = None,
    ) -> SyntheticSendMarker | None:
        self._require_synthetic_sandbox()
        async with self._transaction() as (conn, revision):
            binding = await _admit_synthetic(conn, admit)
            action = _synthetic_marker_action(binding, evidence_contract)
            command = await self._lock_owned(conn, owner)
            if not command:
                return None
            [execution] = await rows(
                conn,
                "SELECT status FROM executions WHERE id=:id",
                id=owner.execution_id,
            )
            if binding is not None:
                if not await rows(
                    conn,
                    "SELECT 1 FROM execution_commands WHERE execution_id=:id AND tool_id=:tool",
                    id=owner.execution_id,
                    tool=_synthetic_tool(evidence_contract),
                ):
                    raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
                await self._lock_synthetic_audit_guards(conn)
            now = await self._clock(conn)
            if execution["status"] != "RUNNING" or now >= min(
                command["lease_expires_at"], command["dispatch_not_after"]
            ):
                return None
            marked = await rows(
                conn,
                """
                UPDATE execution_attempts SET send_marked_at=:now, admission_revision=:revision
                WHERE execution_id=:execution_id AND generation=:generation AND worker_id=:worker_id
                  AND kind='SEND' AND send_marked_at IS NULL AND outcome IS NULL
                RETURNING generation
                """,
                now=now,
                revision=revision,
                **owner.model_dump(),
            )
            if not marked:
                return None
            await execute(
                conn,
                "UPDATE execution_commands SET state='DISPATCHING' WHERE execution_id=:id",
                id=owner.execution_id,
            )
            await self._synthetic_audit(conn, owner, action)
            return SyntheticSendMarker(
                owner=owner, marked_at=now, admission_revision=revision
            )

    async def _lock_marked(
        self, conn: AsyncConnection, owner: ExecutionOwner
    ) -> tuple[dict, dict] | None:
        command = await self._lock_owned(conn, owner, dispatching=True)
        if not command:
            return None
        attempts = await rows(
            conn,
            """
            SELECT send_marked_at, admission_revision FROM execution_attempts
            WHERE execution_id=:execution_id AND generation=:generation AND worker_id=:worker_id
              AND kind='SEND' AND send_marked_at IS NOT NULL AND outcome IS NULL
            FOR UPDATE
            """,
            **owner.model_dump(),
        )
        return (command, attempts[0]) if attempts else None

    @staticmethod
    async def _lock_synthetic_audit_guards(conn: AsyncConnection) -> None:
        # Keep the sandbox guards installed until this transaction commits.
        await execute(conn, "LOCK TABLE public.audit_events IN ROW EXCLUSIVE MODE")
        [guard] = await rows(
            conn,
            """
            SELECT count(*) = 2 AS enabled FROM pg_catalog.pg_trigger
            WHERE tgrelid='public.audit_events'::regclass AND tgenabled='A'
              AND tgfoid=pg_catalog.to_regprocedure('public.protect_synthetic_dispatch_evidence()')
              -- pg_trigger event bits: row/before/insert/update/delete and before/truncate.
              AND ((tgname='protect_synthetic_consumption_rows' AND tgtype=31)
                OR (tgname='protect_synthetic_consumption_truncate' AND tgtype=34))
            """,
        )
        if not guard["enabled"]:
            raise CommandStoreError("SYNTHETIC_CONSUMPTION_PROTECTION_UNAVAILABLE")

    @staticmethod
    async def _verify_marker_contract(
        conn: AsyncConnection,
        owner: ExecutionOwner,
        binding: bytes | None,
        contract: SyntheticEvidenceContract | None,
    ) -> None:
        # The execution and its single marked attempt are already locked.
        matching = await rows(
            conn,
            """
            SELECT actor, action FROM audit_events WHERE resource_id=:id
            AND (action=:marker OR starts_with(action, :prefix))
            """,
            id=owner.execution_id,
            marker=_SYNTHETIC_MARKER_ACTION,
            prefix=f"{_SYNTHETIC_MARKER_ACTION}:",
        )
        if matching != [
            {
                "actor": str(owner.worker_id),
                "action": _synthetic_marker_action(binding, contract),
            }
        ]:
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")

    @staticmethod
    async def _synthetic_send_consumed(
        conn: AsyncConnection, execution_id: UUID
    ) -> bool:
        return bool(
            await rows(
                conn,
                """
                SELECT 1 FROM audit_events WHERE resource_id=:id
                    AND action='SYNTHETIC_INVOCATION_CONSUMED' LIMIT 1
                """,
                id=execution_id,
            )
        )

    async def consume_synthetic_send(
        self,
        marker: SyntheticSendMarker,
        *,
        admit: SyntheticAdmission | None = None,
        evidence_contract: SyntheticEvidenceContract | None = None,
    ) -> bool:
        """Consume invocation permission. Only an acknowledged commit permits a send."""
        self._require_synthetic_sandbox()
        async with self._transaction() as (conn, _):
            binding = await _admit_synthetic(conn, admit)
            locked = await self._lock_marked(conn, marker.owner)
            if not locked:
                return False
            command, attempt = locked
            [execution] = await rows(
                conn,
                "SELECT status FROM executions WHERE id=:id",
                id=marker.owner.execution_id,
            )
            await self._lock_synthetic_audit_guards(conn)
            await self._verify_marker_contract(
                conn, marker.owner, binding, evidence_contract
            )
            if await self._synthetic_send_consumed(conn, marker.owner.execution_id):
                return False
            now = await self._clock(conn)
            if not (
                execution["status"] == "RUNNING"
                and now
                < min(command["lease_expires_at"], command["dispatch_not_after"])
                and attempt["send_marked_at"] == marker.marked_at
                and attempt["admission_revision"] == marker.admission_revision
            ):
                return False
            await self._synthetic_audit(
                conn, marker.owner, "SYNTHETIC_INVOCATION_CONSUMED"
            )
            return True

    def _synthetic_result(
        self, evidence: SyntheticOutcome
    ) -> tuple[ExecutionState, bytes]:
        if (
            evidence.operation_id != evidence.owner.execution_id
            or evidence.result not in ("EFFECT_COMMITTED", "DEFINITIVE_NO_EFFECT")
        ):
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
        target = (
            ExecutionState.SUCCEEDED
            if evidence.result == "EFFECT_COMMITTED"
            else ExecutionState.FAILED
        )
        content = synthetic_evidence_bytes(evidence)
        key = self._synthetic_key
        if key is None or not compare_digest(
            hmac_digest(key, content, "sha256"), evidence.attestation
        ):
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
        return target, sha256(content).digest()

    async def finalize_synthetic(self, evidence: SyntheticOutcome) -> bool:
        self._require_synthetic_sandbox()
        target, digest = self._synthetic_result(evidence)
        async with self._transaction() as (conn, _):
            locked = await self._lock_marked(conn, evidence.owner)
            if not locked:
                return False
            command, _ = locked
            await self._lock_synthetic_audit_guards(conn)
            await self._verify_marker_contract(
                conn,
                evidence.owner,
                evidence.intent_binding,
                evidence.evidence_contract,
            )
            if not await self._synthetic_send_consumed(
                conn, evidence.owner.execution_id
            ):
                return False
            now = await self._clock(conn)
            if now >= command["lease_expires_at"]:
                return False
            await self._finish_marked(
                conn,
                evidence.owner,
                target,
                now,
                digest,
                evidence_source="LOOKUP"
                if evidence.intent_binding is not None
                else "RESPONSE",
            )
            return True

    async def classify_synthetic_unknown(
        self, owner: ExecutionOwner, *, expired: bool = False
    ) -> bool:
        """Classify a marked attempt using either the live or expired fence."""
        self._require_synthetic_sandbox()
        async with self._transaction() as (conn, _):
            locked = await self._lock_marked(conn, owner)
            if not locked:
                return False
            command, _ = locked
            now = await self._clock(conn)
            if (now >= command["lease_expires_at"]) != expired:
                return False
            await self._finish_marked(conn, owner, ExecutionState.UNKNOWN, now, None)
            return True

    async def _lock_refund_send(
        self,
        conn: AsyncConnection,
        execution_id: UUID,
        *,
        outcome: str | None = "UNKNOWN",
    ) -> tuple[ExecutionOwner, bytes] | None:
        """Read the original protected refund marker under the execution lock."""
        sends = await rows(
            conn,
            """
            SELECT a.worker_id, a.generation FROM execution_attempts a
            JOIN execution_commands c USING (execution_id)
            WHERE a.execution_id=:id AND c.tool_id='payments.issue_refund'
              AND a.kind='SEND' AND a.send_marked_at IS NOT NULL
              AND a.outcome IS NOT DISTINCT FROM CAST(:outcome AS text)
            FOR UPDATE OF a
            """,
            id=execution_id,
            outcome=outcome,
        )
        if len(sends) != 1:
            return None
        await self._lock_synthetic_audit_guards(conn)
        markers = await rows(
            conn,
            """
            SELECT action FROM public.audit_events WHERE resource_id=:id
              AND starts_with(action,:prefix)
            """,
            id=execution_id,
            prefix=_REFUND_MARKER_PREFIX,
        )
        if not markers:
            return None
        if len(markers) != 1:
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
        try:
            binding = bytes.fromhex(
                markers[0]["action"].removeprefix(_REFUND_MARKER_PREFIX)
            )
        except ValueError:
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH") from None
        if len(binding) != 32:
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
        sender = ExecutionOwner(execution_id=execution_id, **sends[0])
        await self._verify_marker_contract(conn, sender, binding, None)
        return sender, binding

    async def recover_expired_synthetic_refund(self, owner: ExecutionOwner) -> bool:
        """Park an expired bound refund without issuing or authorizing a send."""
        self._require_synthetic_sandbox()
        async with self._transaction() as (conn, _):
            locked = await self._lock_marked(conn, owner)
            if not locked:
                return False
            original = await self._lock_refund_send(
                conn, owner.execution_id, outcome=None
            )
            if original is None or original[0] != owner:
                return False
            now = await self._clock(conn)
            if now < locked[0]["lease_expires_at"]:
                return False
            await self._finish_marked(
                conn, owner, ExecutionState.UNKNOWN, now, None, next_check_at=now
            )
            return True

    @staticmethod
    async def _lock_refund_unknown(
        conn: AsyncConnection, execution_id: UUID
    ) -> dict | None:
        [interface] = await rows(
            conn, "SELECT to_regclass('m914_refund.windows') IS NOT NULL AS installed"
        )
        if interface["installed"]:
            raise CommandStoreError("REFUND_VERIFICATION_RESTRICTED_INTERFACE_REQUIRED")
        if not await rows(conn, _LOCK_EXECUTION, id=execution_id):
            return None
        commands = await rows(
            conn,
            """
            SELECT c.generation, c.lease_owner, c.lease_expires_at, c.next_check_at
            FROM execution_commands c JOIN executions e ON e.id=c.execution_id
            WHERE c.execution_id=:id AND c.state='UNKNOWN' AND e.status='UNKNOWN'
              AND c.tool_id='payments.issue_refund' AND NOT c.verification_only
            FOR UPDATE OF c
            """,
            id=execution_id,
        )
        return commands[0] if commands else None

    async def claim_synthetic_refund_verification(
        self, execution_id: UUID, worker_id: UUID
    ) -> RefundVerification | None:
        self._require_synthetic_sandbox()
        async with self._transaction() as (conn, _):
            command = await self._lock_refund_unknown(conn, execution_id)
            if command is None:
                return None
            original = await self._lock_refund_send(conn, execution_id)
            if original is None:
                return None
            now = await self._clock(conn)
            due = command["next_check_at"]
            expiry = command["lease_expires_at"]
            if (due is not None and due > now) or (expiry is not None and expiry > now):
                return None
            if expiry is not None:
                expired = ExecutionOwner(
                    execution_id=execution_id,
                    generation=command["generation"],
                    worker_id=command["lease_owner"],
                )
                await self._close_refund_verification(conn, expired, now)
                await self._synthetic_audit(
                    conn, expired, "SYNTHETIC_VERIFY_LEASE_EXPIRED"
                )
            owner = ExecutionOwner(
                execution_id=execution_id,
                generation=command["generation"] + 1,
                worker_id=worker_id,
            )
            expires_at = now + self._lease_duration
            await execute(
                conn,
                """
                UPDATE execution_commands SET generation=:generation, lease_owner=:worker_id,
                    lease_expires_at=:expires, next_check_at=NULL WHERE execution_id=:execution_id
                """,
                expires=expires_at,
                **owner.model_dump(),
            )
            await execute(
                conn,
                """
                INSERT INTO execution_attempts(execution_id,generation,worker_id,kind,claimed_at)
                VALUES (:execution_id,:generation,:worker_id,'VERIFY',:now)
                """,
                now=now,
                **owner.model_dump(),
            )
            await self._synthetic_audit(conn, owner, "SYNTHETIC_VERIFY_CLAIMED")
            return RefundVerification(
                lease=ExecutionLease(owner=owner, expires_at=expires_at),
                sender=original[0],
                intent_binding=original[1],
            )

    @staticmethod
    async def _close_refund_verification(
        conn: AsyncConnection, owner: ExecutionOwner, now: datetime
    ) -> None:
        closed = await rows(
            conn,
            """
            UPDATE execution_attempts SET outcome='UNKNOWN', finished_at=:now
            WHERE execution_id=:execution_id AND generation=:generation AND worker_id=:worker_id
              AND kind='VERIFY' AND outcome IS NULL RETURNING generation
            """,
            now=now,
            **owner.model_dump(),
        )
        if not closed:
            raise CommandStoreError("EXECUTION_OWNERSHIP_CONFLICT")

    async def finish_synthetic_refund_verification(
        self,
        claim: RefundVerification,
        *,
        evidence: SyntheticOutcome | None = None,
        binding_preimage: bytes | None = None,
        defer_for: timedelta = timedelta(seconds=5),
        deferral_reason: RefundDeferralReason = "NO_EVIDENCE",
    ) -> ExecutionState | None:
        self._require_synthetic_sandbox()
        if defer_for <= timedelta(0):
            raise ValueError("verification deferral must be positive")
        if (
            binding_preimage is not None
            and sha256(binding_preimage).digest() != claim.intent_binding
        ):
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
        owner = claim.lease.owner
        async with self._transaction() as (conn, _):
            command = await self._lock_refund_unknown(conn, owner.execution_id)
            if command is None or (
                command["generation"] != owner.generation
                or command["lease_owner"] != owner.worker_id
                or command["lease_expires_at"] != claim.lease.expires_at
            ):
                return None
            original = await self._lock_refund_send(conn, owner.execution_id)
            if original != (claim.sender, claim.intent_binding):
                raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
            attempts = await rows(
                conn,
                """
                SELECT generation FROM execution_attempts
                WHERE execution_id=:execution_id AND generation=:generation
                  AND worker_id=:worker_id AND kind='VERIFY' AND outcome IS NULL
                FOR UPDATE
                """,
                **owner.model_dump(),
            )
            now = await self._clock(conn)
            if not attempts or now >= command["lease_expires_at"]:
                return None
            if evidence is not None:
                consumed = await rows(
                    conn,
                    """
                    SELECT actor FROM public.audit_events WHERE resource_id=:id
                      AND action='SYNTHETIC_INVOCATION_CONSUMED'
                    """,
                    id=owner.execution_id,
                )
                if (
                    evidence.owner != claim.sender
                    or evidence.intent_binding != claim.intent_binding
                    or evidence.evidence_contract is not None
                    or consumed != [{"actor": str(claim.sender.worker_id)}]
                ):
                    raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
                target, digest = self._synthetic_result(evidence)
                await self._finish_marked(
                    conn,
                    owner,
                    target,
                    now,
                    digest,
                    evidence_source="LOOKUP",
                    expected_state=ExecutionState.UNKNOWN,
                )
                return target
            await self._close_refund_verification(conn, owner, now)
            await execute(
                conn,
                """
                UPDATE execution_commands SET lease_owner=NULL, lease_expires_at=NULL,
                    next_check_at=:due WHERE execution_id=:id
                """,
                due=now + defer_for,
                id=owner.execution_id,
            )
            await self._synthetic_audit(
                conn, owner, f"SYNTHETIC_VERIFY_DEFERRED_{deferral_reason}"
            )
            return ExecutionState.UNKNOWN

    async def _finish_marked(
        self,
        conn: AsyncConnection,
        owner: ExecutionOwner,
        target: ExecutionState,
        now: datetime,
        digest: bytes | None,
        *,
        evidence_source: Literal["RESPONSE", "LOOKUP"] = "RESPONSE",
        expected_state: ExecutionState = ExecutionState.DISPATCHING,
        next_check_at: datetime | None = None,
    ) -> None:
        finished = await rows(
            conn,
            """
            UPDATE execution_attempts SET outcome=:outcome, finished_at=:now
            WHERE execution_id=:execution_id AND generation=:generation AND worker_id=:worker_id
              AND kind=:kind AND outcome IS NULL
            RETURNING generation
            """,
            outcome=target.value,
            now=now,
            kind="VERIFY" if expected_state is ExecutionState.UNKNOWN else "SEND",
            **owner.model_dump(),
        )
        if not finished:
            raise CommandStoreError("EXECUTION_OWNERSHIP_CONFLICT")
        if digest is not None:
            await execute(
                conn,
                """
                INSERT INTO execution_receipts (execution_id, generation, outcome,
                    evidence_source, external_reference, evidence_digest, recorded_at)
                VALUES (:id, :generation, :outcome, :evidence_source, :reference, :digest, :now)
                """,
                id=owner.execution_id,
                generation=owner.generation,
                outcome=target.value,
                evidence_source=evidence_source,
                reference=f"synthetic:{owner.execution_id}",
                digest=digest,
                now=now,
            )
        await execute(
            conn,
            """
            UPDATE execution_commands SET state=:state, lease_owner=NULL, lease_expires_at=NULL,
                next_check_at=:next_check_at WHERE execution_id=:id
            """,
            id=owner.execution_id,
            state=target.value,
            next_check_at=next_check_at,
        )
        error = {
            ExecutionState.SUCCEEDED: None,
            ExecutionState.FAILED: "SYNTHETIC_DEFINITIVE_NO_EFFECT",
            ExecutionState.UNKNOWN: "UNKNOWN_OUTCOME",
        }[target]
        result = await rows(
            conn,
            """
            UPDATE executions SET status=:status, error_code=:error, finished_at=:now
            WHERE id=:id AND status=:expected_status RETURNING id
            """,
            id=owner.execution_id,
            status=target.public_status,
            expected_status=expected_state.public_status,
            error=error,
            now=now,
        )
        if not result:
            raise CommandStoreError("EXECUTION_OWNERSHIP_CONFLICT")
        action_prefix = (
            "SYNTHETIC_VERIFY"
            if expected_state is ExecutionState.UNKNOWN
            else "SYNTHETIC"
        )
        await self._synthetic_audit(conn, owner, f"{action_prefix}_{target.value}")

    async def read_synthetic_attempt(
        self, owner: ExecutionOwner, evidence: SyntheticOutcome | None = None
    ) -> dict | None:
        """Read the primary after a lost acknowledgment. This grants no send."""
        self._require_synthetic_sandbox()
        if evidence is not None and evidence.owner != owner:
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
        expected = self._synthetic_result(evidence) if evidence is not None else None
        source: Literal["RESPONSE", "LOOKUP"] = (
            "LOOKUP"
            if evidence is not None and evidence.intent_binding is not None
            else "RESPONSE"
        )
        return await self._read_synthetic_attempt(owner, expected, source)

    async def read_completed_synthetic_refund_verification(
        self, claim: RefundVerification, evidence: SyntheticOutcome
    ) -> ExecutionState | None:
        """Read back a canonical result after an ambiguous verification commit."""
        self._require_synthetic_sandbox()
        if (
            evidence.owner != claim.sender
            or evidence.intent_binding != claim.intent_binding
            or evidence.evidence_contract is not None
        ):
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
        owner = claim.lease.owner
        observed = await self._read_synthetic_attempt(
            owner, self._synthetic_result(evidence), "LOOKUP"
        )
        if (
            observed is not None
            and observed["receipt_matches"]
            and observed["kind"] == "VERIFY"
            and observed["current_generation"] == owner.generation
        ):
            return observed["state"]
        return None

    async def _read_synthetic_attempt(
        self,
        owner: ExecutionOwner,
        expected: tuple[ExecutionState, bytes] | None,
        source: Literal["RESPONSE", "LOOKUP"],
    ) -> dict | None:
        try:
            async with self._db.connect() as conn:
                result = await rows(
                    conn,
                    """
                    SELECT c.state, c.generation AS current_generation, e.status,
                           a.kind, a.send_marked_at, a.outcome, r.evidence_digest,
                           r.outcome AS receipt_outcome, r.evidence_source, r.external_reference
                    FROM execution_attempts a JOIN execution_commands c USING (execution_id)
                    JOIN executions e ON e.id=a.execution_id
                    LEFT JOIN execution_receipts r ON r.execution_id=a.execution_id
                        AND r.generation=a.generation
                    WHERE a.execution_id=:execution_id AND a.generation=:generation
                        AND a.worker_id=:worker_id
                    """,
                    **owner.model_dump(),
                )
        except (SQLAlchemyError, OSError, TimeoutError):
            raise CommandStoreError("EXECUTION_OWNERSHIP_UNAVAILABLE") from None
        if not result:
            return None
        item = result[0]
        return {
            "kind": item["kind"],
            "state": ExecutionState(item["state"]),
            "current_generation": item["current_generation"],
            "marked": item["send_marked_at"] is not None,
            "outcome": item["outcome"],
            "receipt_matches": expected is not None
            and (
                item["state"],
                item["status"],
                item["outcome"],
                item["receipt_outcome"],
                item["evidence_digest"],
                item["evidence_source"],
                item["external_reference"],
            )
            == (
                expected[0],
                expected[0].public_status,
                expected[0],
                expected[0],
                expected[1],
                source,
                f"synthetic:{owner.execution_id}",
            ),
        }
