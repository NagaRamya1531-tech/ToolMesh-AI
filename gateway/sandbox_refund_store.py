"""Client for the disposable refund verifier's restricted PostgreSQL interface."""

from datetime import timedelta
from hashlib import sha256
from hmac import compare_digest, digest
from uuid import UUID

from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.execution_approval import ExecutionApprovals, LockedApproval
from gateway.execution_contracts import (
    ExecutionLease,
    ExecutionOwner,
    ExecutionState,
    ProtectedIntentScope,
    RefundDeferralReason,
    RefundVerification,
    SyntheticOutcome,
)
from gateway.execution_crypto import PayloadUnavailable
from gateway.execution_store import CommandStoreError
from gateway.execution_worker_store import synthetic_evidence_bytes
from shared.database import execute, rows

_OPERATE = """
    SELECT * FROM m914_refund.operate(
        :action,:id,:worker,:generation,:lease_ms,:preimage,:verdict,:reason
    )
"""
_SQL_ERRORS = {
    "M9141": "WORKER_SCHEMA_UNSUPPORTED",
    "M9142": "EXECUTION_COORDINATION_UNAVAILABLE",
    "M9143": "DURABLE_ADMISSION_DISABLED",
    "M9144": "EXECUTION_OWNERSHIP_CONFLICT",
    "M9145": "WORKER_ISOLATION_UNSUPPORTED",
    "M9146": "SYNTHETIC_CONSUMPTION_PROTECTION_UNAVAILABLE",
    "M9147": "SYNTHETIC_EVIDENCE_MISMATCH",
    "M9148": "SYNTHETIC_OPERATOR_AUDIT_PROTECTION_UNAVAILABLE",
    "42501": "WORKER_ACCESS_DENIED",
    "22023": "WORKER_CONFIGURATION_REJECTED",
}


class SandboxRefundStore:
    def __init__(
        self,
        db: AsyncEngine,
        approvals: ExecutionApprovals,
        lease_duration: timedelta,
        evidence_key: bytes,
    ):
        self._db = db
        self._approvals = approvals
        self._lease_ms = int(lease_duration / timedelta(milliseconds=1))
        if not 1 <= self._lease_ms <= 300000 or len(evidence_key) != 32:
            raise ValueError("invalid sandbox verifier configuration")
        if not db.sync_engine.hide_parameters or db.echo:
            raise ValueError("verifier connections must hide SQL parameters")
        self._evidence_key = evidence_key

    async def _request(
        self, sql: str, *, read_only: bool = False, **params: object
    ) -> list[dict]:
        try:
            async with self._db.begin() as conn:
                await execute(
                    conn,
                    "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
                    + (" READ ONLY" if read_only else ""),
                )
                await execute(conn, "SET LOCAL lock_timeout = '2s'")
                await execute(conn, "SET LOCAL statement_timeout = '3s'")
                return await rows(conn, sql, **params)
        except DBAPIError as error:
            code = str(getattr(error.orig, "sqlstate", ""))
            raise CommandStoreError(
                _SQL_ERRORS.get(code, "EXECUTION_OWNERSHIP_UNAVAILABLE")
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise CommandStoreError("EXECUTION_OWNERSHIP_UNAVAILABLE") from None

    async def validate_access(self) -> None:
        """Check the actual login, memberships, grants, and installed endpoints."""
        try:
            records = await self._request(
                """
                WITH permitted(name) AS (VALUES
                    ('m914_ownership.register_worker()'),
                    ('m914_ownership.operate(text,uuid,uuid,bigint,integer)'),
                    ('m914_refund.discover(integer)'),
                    ('m914_refund.operate(text,uuid,uuid,bigint,integer,bytea,text,text)'),
                    ('m914_refund.intent(uuid,uuid,bigint)'),
                    ('m914_refund.receipt(uuid,uuid,bigint)')
                )
                SELECT current_user=session_user
                  AND pg_has_role(current_user,'m914_refund_verifier','USAGE')
                  AND pg_has_role(current_user,'m914_worker_access','USAGE')
                  AND NOT has_database_privilege(current_user,current_database(),'CREATE')
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_roles r WHERE pg_has_role(current_user,r.oid,'MEMBER')
                    AND (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
                         OR r.rolbypassrls
                         OR r.rolname NOT IN (current_user,'m914_refund_verifier','m914_worker_access'))
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_namespace n
                    WHERE n.nspname IN ('public','m914_ownership','m914_refund','synthetic_refund')
                      AND has_schema_privilege(current_user,n.oid,'CREATE')
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname IN ('public','synthetic_refund','m914_ownership','m914_refund')
                      AND c.relkind IN ('r','p','v','m','f')
                      AND (has_any_column_privilege(current_user,c.oid,'INSERT,UPDATE,REFERENCES')
                        OR has_table_privilege(current_user,c.oid,'DELETE,TRUNCATE,TRIGGER')
                        OR (has_any_column_privilege(current_user,c.oid,'SELECT')
                          AND (n.nspname,c.relname) NOT IN
                            (('public','alembic_version'),('synthetic_refund','outcomes'))))
                  )
                  AND has_schema_privilege(current_user,'m914_refund','USAGE')
                  AND has_schema_privilege(current_user,'synthetic_refund','USAGE')
                  AND has_column_privilege(current_user,'synthetic_refund.outcomes','operation_id','SELECT')
                  AND has_column_privilege(current_user,'synthetic_refund.outcomes','binding','SELECT')
                  AND has_column_privilege(current_user,'synthetic_refund.outcomes','refunded_minor','SELECT')
                  AND has_function_privilege(current_user,'m914_ownership.register_worker()','EXECUTE')
                  AND has_function_privilege(current_user,'m914_ownership.operate(text,uuid,uuid,bigint,integer)','EXECUTE')
                  AND NOT EXISTS (
                    SELECT 1 FROM permitted required
                    LEFT JOIN pg_proc p ON p.oid=to_regprocedure(required.name)
                    LEFT JOIN pg_roles r ON r.oid=p.proowner
                    WHERE p.oid IS NULL OR NOT p.prosecdef
                      OR NOT has_function_privilege(current_user,p.oid,'EXECUTE')
                      OR r.rolname<>CASE WHEN starts_with(required.name,'m914_refund.')
                         THEN 'm914_refund_function_owner' ELSE 'm914_function_owner' END
                      OR r.rolcanlogin
                      OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname IN ('public','synthetic_refund','m914_ownership','m914_refund')
                      AND p.prosecdef AND has_function_privilege(current_user,p.oid,'EXECUTE')
                      AND NOT EXISTS (
                        SELECT 1 FROM permitted WHERE p.oid=to_regprocedure(name)
                      )
                  ) AS allowed
                """,
                read_only=True,
            )
        except CommandStoreError:
            raise CommandStoreError("WORKER_VERIFIER_ACCESS_REJECTED") from None
        if records != [{"allowed": True}]:
            raise CommandStoreError("WORKER_VERIFIER_ACCESS_REJECTED")

    async def discover_due_synthetic_refunds(self, *, limit: int = 8) -> list[UUID]:
        if not 1 <= limit <= 8:
            raise ValueError("refund discovery limit must be between 1 and 8")
        try:
            candidates = await self._request(
                "SELECT execution_id FROM m914_refund.discover(:limit)",
                read_only=True,
                limit=limit,
            )
        except CommandStoreError as error:
            if str(error) != "EXECUTION_OWNERSHIP_UNAVAILABLE":
                raise
            raise CommandStoreError(
                "REFUND_VERIFICATION_DISCOVERY_UNAVAILABLE"
            ) from None
        return [row["execution_id"] for row in candidates]

    async def claim_synthetic_refund_verification(
        self,
        execution_id: UUID,
        worker_id: UUID,
    ) -> RefundVerification | None:
        records = await self._request(
            _OPERATE,
            action="claim",
            id=execution_id,
            worker=worker_id,
            generation=None,
            lease_ms=self._lease_ms,
            preimage=None,
            verdict=None,
            reason=None,
        )
        if not records:
            return None
        [row] = records
        return RefundVerification(
            lease=ExecutionLease(
                owner=ExecutionOwner(
                    execution_id=execution_id,
                    worker_id=worker_id,
                    generation=row["generation"],
                ),
                expires_at=row["expires_at"],
            ),
            sender=ExecutionOwner(
                execution_id=execution_id,
                worker_id=row["sender_worker"],
                generation=row["sender_generation"],
            ),
            intent_binding=row["intent_binding"],
            lookup_timeout_ms=row["lookup_timeout_ms"],
        )

    async def read_intent(
        self,
        claim: RefundVerification,
    ) -> tuple[ProtectedIntentScope, dict, bytes]:
        try:
            records = await self._request(
                "SELECT * FROM m914_refund.intent(:execution_id,:worker_id,:generation)",
                read_only=True,
                **claim.lease.owner.model_dump(),
            )
        except CommandStoreError as error:
            if str(error) != "EXECUTION_OWNERSHIP_UNAVAILABLE":
                raise
            raise CommandStoreError("APPROVAL_STORE_UNAVAILABLE") from None
        if not records:
            raise CommandStoreError("APPROVAL_NOT_AVAILABLE")
        [item] = records
        command, reservation = item["command"], item["reservation"]
        for record, fields in (
            (command, ("contract_digest", "ciphertext", "wrapped_data_key", "nonce")),
            (reservation, ("key_digest", "intent_digest")),
        ):
            for field in fields:
                value = record[field]
                if value is None:
                    raise CommandStoreError("EXECUTION_PAYLOAD_UNAVAILABLE")
                record[field] = bytes.fromhex(value[2:])
        locked = LockedApproval(
            command=command,
            reservation=reservation,
            approval=item["approval"],
            execution=item["execution"],
            catalog={"id": command["tool_id"]},
        )
        try:
            scope, arguments = self._approvals.verify_sealed_intent(
                claim.sender.execution_id, locked, item["contract"]
            )
        except PayloadUnavailable:
            raise CommandStoreError("EXECUTION_PAYLOAD_UNAVAILABLE") from None
        return scope, arguments, reservation["intent_digest"]

    def _evidence_digest(
        self, claim: RefundVerification, evidence: SyntheticOutcome
    ) -> bytes:
        if (
            evidence.owner != claim.sender
            or evidence.operation_id != claim.sender.execution_id
            or evidence.intent_binding != claim.intent_binding
            or evidence.evidence_contract is not None
        ):
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
        content = synthetic_evidence_bytes(evidence)
        if not compare_digest(
            digest(self._evidence_key, content, "sha256"), evidence.attestation
        ):
            raise CommandStoreError("SYNTHETIC_EVIDENCE_MISMATCH")
        return sha256(content).digest()

    async def finish_synthetic_refund_verification(
        self,
        claim: RefundVerification,
        *,
        evidence: SyntheticOutcome | None = None,
        binding_preimage: bytes | None = None,
        deferral_reason: RefundDeferralReason = "NO_EVIDENCE",
    ) -> ExecutionState | None:
        if evidence is not None:
            self._evidence_digest(claim, evidence)
        owner = claim.lease.owner
        records = await self._request(
            _OPERATE,
            action="finish",
            id=owner.execution_id,
            worker=owner.worker_id,
            generation=owner.generation,
            lease_ms=None,
            preimage=binding_preimage,
            verdict=evidence.result if evidence is not None else None,
            reason=deferral_reason,
        )
        return ExecutionState(records[0]["state"]) if records else None

    async def read_completed_synthetic_refund_verification(
        self,
        claim: RefundVerification,
        evidence: SyntheticOutcome,
    ) -> ExecutionState | None:
        expected = self._evidence_digest(claim, evidence)
        records = await self._request(
            "SELECT * FROM m914_refund.receipt(:execution_id,:worker_id,:generation)",
            read_only=True,
            **claim.lease.owner.model_dump(),
        )
        if records and compare_digest(records[0]["evidence_digest"], expected):
            return ExecutionState(records[0]["state"])
        return None
