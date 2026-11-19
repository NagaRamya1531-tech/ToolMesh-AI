"""Dormant Slice A command admission. No API, workers, or adapter dispatch."""

from asyncio import sleep
from dataclasses import dataclass, field, replace
from hashlib import sha256
from hmac import compare_digest
from random import uniform
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, SchemaError
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from gateway.access import POLICY, object_allowed, scope_params
from gateway.execution_contracts import (
    ApprovalState,
    ProtectedIntentScope,
    ProtectedTool,
)
from gateway.execution_crypto import (
    CommandDigests,
    PayloadContext,
    PayloadProtection,
    PayloadUnavailable,
    canonical_json,
)
from gateway.identity import resolve_principal
from shared.database import execute, rows

CANONICALIZER_VERSION = 1
RETRYABLE_SQLSTATES = {"40001", "40P01", "55P03"}


class CommandStoreError(Exception):
    """Stable error code safe for an eventual caller-facing boundary."""


@dataclass(frozen=True)
class CommandSubmission:
    workspace_id: str
    requester: str
    tool_id: ProtectedTool
    arguments: dict = field(repr=False)
    idempotency_key: str = field(repr=False)
    version: int | None = None
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class StoredSubmission:
    execution_id: UUID
    version: int
    status: str
    approval_status: ApprovalState
    replayed: bool


def canonical_arguments(contract: dict, arguments: dict) -> bytes:
    """V1 supports the closed scalar object schemas used by Slice A."""
    schema = contract.get("input_schema")
    if not isinstance(schema, dict):
        raise CommandStoreError("VERSION_INCOMPATIBLE")
    properties = schema.get("properties")
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or not isinstance(properties, dict)
        or any(
            not isinstance(prop, dict)
            or prop.get("type") not in ("string", "integer")
            or set(prop)
            - {
                "type",
                "title",
                "minLength",
                "maxLength",
                "pattern",
                "minimum",
                "maximum",
            }
            for prop in properties.values()
        )
        or set(schema)
        - {"type", "title", "properties", "required", "additionalProperties"}
    ):
        raise CommandStoreError("VERSION_INCOMPATIBLE")
    if set(arguments) - properties.keys() or any(
        type(value) not in {str, int} for value in arguments.values()
    ):
        raise CommandStoreError("INVALID_ARGUMENT")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise CommandStoreError("VERSION_INCOMPATIBLE") from None
    if not Draft202012Validator(schema).is_valid(arguments):
        raise CommandStoreError("INVALID_ARGUMENT")
    try:
        return canonical_json(arguments)
    except (ValueError, TypeError):
        raise CommandStoreError("INVALID_ARGUMENT") from None


class CommandStore:
    def __init__(
        self, db: AsyncEngine, payloads: PayloadProtection, digests: CommandDigests
    ):
        if not db.sync_engine.hide_parameters or db.echo:
            raise ValueError(
                "command store requires hidden SQL parameters and SQL echo off"
            )
        self._db = db
        self._payloads = payloads
        self._digests = digests

    async def submit(self, request: CommandSubmission) -> StoredSubmission:
        """Confirm a commit, or recover by key without repeating the write."""
        if not isinstance(request.idempotency_key, str) or not (
            1 <= len(request.idempotency_key) <= 128
        ):
            raise CommandStoreError("IDEMPOTENCY_KEY_REQUIRED")
        if (
            not isinstance(request.arguments, dict)
            or not isinstance(request.workspace_id, str)
            or not 1 <= len(request.workspace_id) <= 64
            or not isinstance(request.requester, str)
            or not request.requester
            or not isinstance(request.fields, tuple)
            or any(not isinstance(name, str) for name in request.fields)
            or (
                request.version is not None
                and (
                    type(request.version) is not int
                    or not 1 <= request.version <= 2_147_483_647
                )
            )
        ):
            raise CommandStoreError("INVALID_ARGUMENT")
        try:
            request.idempotency_key.encode("utf-8")
        except UnicodeError:
            raise CommandStoreError("INVALID_ARGUMENT") from None
        # The caller owns its input dict. Snapshot it before the first await;
        # the pinned Slice A schema only accepts scalar values.
        request = replace(request, arguments=dict(request.arguments))
        if request.tool_id not in {"payments.issue_refund", "it.restart_service"}:
            raise CommandStoreError("TOOL_NOT_AVAILABLE")
        for attempt in range(3):
            try:
                return await self._write_or_confirm(request)
            except PayloadUnavailable:
                raise CommandStoreError("EXECUTION_PAYLOAD_UNAVAILABLE") from None
            except DBAPIError as exc:
                # Only errors before commit reach this branch. Retry the
                # database transaction, never an ambiguous commit or a send.
                if (
                    attempt == 2
                    or getattr(exc.orig, "sqlstate", None) not in RETRYABLE_SQLSTATES
                ):
                    raise CommandStoreError("COMMAND_STORE_UNAVAILABLE") from None
                await sleep(uniform(0.01, 0.05) * (attempt + 1))
            except (SQLAlchemyError, OSError, TimeoutError):
                raise CommandStoreError("COMMAND_STORE_UNAVAILABLE") from None
        raise AssertionError("admission retry loop exhausted")

    async def _write_or_confirm(self, request: CommandSubmission) -> StoredSubmission:
        ambiguous = False
        try:
            async with self._db.connect() as conn:
                # Apply this on the checked-out connection, overriding caller
                # defaults before beginning any transaction or writing rows.
                await conn.execution_options(isolation_level="READ COMMITTED")
                transaction = await conn.begin()
                result = await self._admit(conn, request, allow_insert=True)
                try:
                    await transaction.commit()
                except (SQLAlchemyError, OSError, TimeoutError):
                    ambiguous = True
                else:
                    return result
        except (SQLAlchemyError, OSError, TimeoutError):
            if not ambiguous:
                raise

        try:
            # Reenter through the same gate, policy and reservation locks on a
            # new connection, which must support read/write row locks.
            async with self._db.connect() as conn:
                await conn.execution_options(isolation_level="READ COMMITTED")
                async with conn.begin():
                    return await self._admit(conn, request, allow_insert=False)
        except (CommandStoreError, SQLAlchemyError, OSError, TimeoutError):
            raise CommandStoreError("ADMISSION_COMMIT_UNCONFIRMED") from None

    async def _admit(
        self, conn: AsyncConnection, request: CommandSubmission, *, allow_insert: bool
    ) -> StoredSubmission:
        await execute(conn, "SET LOCAL lock_timeout = '2s'")
        gate = await rows(
            conn, "SELECT mode FROM execution_dispatch_gate WHERE singleton FOR SHARE"
        )
        guard = await rows(
            conn,
            "SELECT revision FROM execution_authorization_guard WHERE singleton FOR SHARE",
        )
        if not gate or not guard:
            raise CommandStoreError("EXECUTION_COORDINATION_UNAVAILABLE")
        if gate[0]["mode"] != "DURABLE":
            raise CommandStoreError("DURABLE_ADMISSION_DISABLED")

        principal = await resolve_principal(conn, request.requester)
        if principal is None or not object_allowed(principal, request.arguments):
            raise CommandStoreError("TOOL_NOT_AVAILABLE")
        scope = ProtectedIntentScope(
            workspace_id=request.workspace_id,
            requester=principal.id,
            customer_id=principal.customer,
            tool_id=request.tool_id,
        )
        catalog = await rows(
            conn,
            "SELECT active_version, lifecycle FROM tools WHERE id=:tool FOR SHARE",
            tool=scope.tool_id,
        )
        if not catalog:
            raise CommandStoreError("TOOL_NOT_AVAILABLE")

        key_digest = self._digests.lookup(scope, request.idempotency_key)
        alias_digest = self._digests.legacy_alias(
            scope.requester, scope.tool_id, request.idempotency_key
        )
        # Serialize both namespaces before looking for an absent key. PostgreSQL
        # uniqueness remains the durable boundary; lock collisions only serialize.
        await execute(
            conn,
            "SELECT pg_advisory_xact_lock(:key)",
            key=int.from_bytes(alias_digest[:8], "big", signed=True),
        )
        matches = await rows(
            conn,
            """
            SELECT k.execution_id, k.workspace_id, k.customer_id, k.tool_version,
                   k.intent_digest, k.digest_key_version, k.canonicalizer_version,
                   c.contract_digest, e.status, a.status AS approval_status
            FROM execution_request_keys k
            JOIN executions e ON e.id=k.execution_id
            JOIN approvals a ON a.id=k.execution_id
            LEFT JOIN execution_commands c ON c.execution_id=k.execution_id
            WHERE k.requester=:requester AND k.tool_id=:tool AND (
                (k.workspace_id=:workspace AND k.customer_id=:customer AND k.key_digest=:key)
                OR k.execution_id=(SELECT execution_id FROM execution_legacy_key_aliases
                    WHERE requester=:requester AND tool_id=:tool AND key_digest=:alias)
            )
            """,
            requester=scope.requester,
            tool=scope.tool_id,
            workspace=scope.workspace_id,
            customer=scope.customer_id,
            key=key_digest,
            alias=alias_digest,
        )
        if len(matches) > 1 or any(
            (item["workspace_id"], item["customer_id"])
            != (scope.workspace_id, scope.customer_id)
            for item in matches
        ):
            raise CommandStoreError("IDEMPOTENCY_CONFLICT")
        prior = matches[0] if matches else None
        if prior is None and not allow_insert:
            raise CommandStoreError("ADMISSION_COMMIT_UNCONFIRMED")
        version = prior["tool_version"] if prior else catalog[0]["active_version"]
        if version is None or (
            prior is None and catalog[0]["lifecycle"] != "published"
        ):
            raise CommandStoreError("TOOL_NOT_AVAILABLE")
        versions = await rows(
            conn,
            """
            SELECT v.contract FROM tool_versions v JOIN tools t ON t.id=v.tool_id
            WHERE v.tool_id=:tool AND v.version=:version AND v.contract->>'risk'='approval'
            AND
            """
            + POLICY,
            tool=scope.tool_id,
            version=version,
            **scope_params(principal),
        )
        if not versions:
            raise CommandStoreError("TOOL_NOT_AVAILABLE")
        if request.version is not None and request.version != version:
            code = "IDEMPOTENCY_CONFLICT" if prior else "VERSION_INCOMPATIBLE"
            raise CommandStoreError(code)

        contract = versions[0]["contract"]
        arguments = canonical_arguments(contract, request.arguments)
        if request.fields and not set(request.fields).issubset(
            contract.get("output_schema", {}).get("properties", {})
        ):
            raise CommandStoreError("INVALID_ARGUMENT")
        contract_digest = sha256(canonical_json(contract)).digest()
        intent_digest = self._digests.intent(
            canonical_json(
                [
                    CANONICALIZER_VERSION,
                    scope.model_dump(),
                    version,
                    contract_digest.hex(),
                    arguments.decode(),
                ]
            )
        )
        if prior:
            if (
                prior["digest_key_version"] != self._digests.version
                or prior["canonicalizer_version"] != CANONICALIZER_VERSION
                or (
                    prior["contract_digest"] is not None
                    and prior["contract_digest"] != contract_digest
                )
            ):
                raise CommandStoreError("VERSION_INCOMPATIBLE")
            if not compare_digest(prior["intent_digest"], intent_digest):
                raise CommandStoreError("IDEMPOTENCY_CONFLICT")
            return StoredSubmission(
                prior["execution_id"],
                version,
                prior["status"],
                ApprovalState(prior["approval_status"]),
                replayed=True,
            )

        # An unimported legacy reservation must never become a second command.
        # Bind a transient hash, not the raw client key, to this lookup.
        legacy_match = sha256(request.idempotency_key.encode()).digest()
        legacy = await rows(
            conn,
            """
            SELECT 1 FROM approvals a
            WHERE a.requester=:requester AND a.tool_id=:tool
              AND sha256(convert_to(a.request_key, 'UTF8'))=:legacy_match
              AND NOT EXISTS (SELECT 1 FROM execution_commands c WHERE c.execution_id=a.id)
            LIMIT 1
            """,
            requester=scope.requester,
            tool=scope.tool_id,
            legacy_match=legacy_match,
        )
        if legacy:
            raise CommandStoreError("LEGACY_RESERVATION_UNAVAILABLE")
        return await self._persist(
            conn, scope, version, contract_digest, arguments, key_digest, intent_digest
        )

    async def _persist(
        self,
        conn: AsyncConnection,
        scope: ProtectedIntentScope,
        version: int,
        contract_digest: bytes,
        arguments: bytes,
        key_digest: bytes,
        intent_digest: bytes,
    ) -> StoredSubmission:
        execution_id = uuid4()
        sealed = self._payloads.seal(
            arguments,
            PayloadContext("command", execution_id, scope, CANONICALIZER_VERSION),
        )
        routing = {
            "id": execution_id,
            "workspace": scope.workspace_id,
            "requester": scope.requester,
            "customer": scope.customer_id,
            "tool": scope.tool_id,
            "version": version,
        }
        await execute(
            conn,
            """
            INSERT INTO executions (id, principal, tool_id, version, status)
            VALUES (:id,:requester,:tool,:version,'PENDING_APPROVAL')
            """,
            **routing,
        )
        # Existing NOT NULL legacy columns hold only placeholders and keyed
        # evidence. The isolated store is the only reader of the sealed intent.
        await execute(
            conn,
            """
            INSERT INTO approvals (
                id, requester, tool_id, version, customer_id, arguments,
                intent_hash, request_key, status
            ) VALUES (:id,:requester,:tool,:version,:customer,'{}'::jsonb,
                      :intent,:placeholder,'PENDING')
            """,
            **routing,
            intent=intent_digest.hex(),
            placeholder=f"m9-sealed:{key_digest.hex()}",
        )
        await execute(
            conn,
            """
            INSERT INTO execution_request_keys (
                execution_id, workspace_id, requester, customer_id, tool_id,
                tool_version, key_digest, intent_digest, digest_key_version, canonicalizer_version
            ) VALUES (:id,:workspace,:requester,:customer,:tool,:version,
                      :key,:intent,:digest_version,:canonicalizer)
            """,
            **routing,
            key=key_digest,
            intent=intent_digest,
            digest_version=self._digests.version,
            canonicalizer=CANONICALIZER_VERSION,
        )
        await execute(
            conn,
            """
            INSERT INTO execution_commands (
                execution_id, workspace_id, requester, customer_id, tool_id, tool_version,
                contract_digest, state, ciphertext, wrapped_data_key, nonce, key_version
            ) VALUES (:id,:workspace,:requester,:customer,:tool,:version,:contract_digest,
                      'PENDING_APPROVAL',:ciphertext,:wrapped_data_key,:nonce,:key_version)
            """,
            **routing,
            contract_digest=contract_digest,
            **sealed.model_dump(),
        )
        await execute(
            conn,
            "INSERT INTO audit_events (actor, action, resource_id) "
            "VALUES (:requester, 'DURABLE_APPROVAL_REQUESTED', :id)",
            **routing,
        )
        return StoredSubmission(
            execution_id,
            version,
            "PENDING_APPROVAL",
            ApprovalState.PENDING,
            replayed=False,
        )
