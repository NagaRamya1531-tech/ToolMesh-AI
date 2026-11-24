"""Disposable PostgreSQL simulator and orchestration entry point for IT restarts."""

from sqlalchemy.ext.asyncio import AsyncEngine
from synthetic_lookup_flow import SyntheticLookupFlow

from gateway.execution_approval import ExecutionApprovals
from gateway.execution_contracts import SyntheticOutcome
from gateway.execution_store import CommandStoreError, lock_durable_coordination
from gateway.execution_worker_store import ExecutionWorkerStore
from gateway.sandbox_restart_evidence import CONTRACT as CONTRACT
from gateway.sandbox_restart_evidence import TOOL as TOOL
from gateway.sandbox_restart_evidence import RestartIntent as RestartIntent
from gateway.sandbox_restart_evidence import RestartRequest as RestartRequest
from gateway.sandbox_restart_evidence import read_restart_evidence
from gateway.sandbox_restart_evidence import restart_intent as restart_intent
from shared.database import execute, rows


class RestartLedger:
    def __init__(self, db: AsyncEngine, key: bytes):
        self._db = db
        self._key = key

    async def initialize(self) -> None:
        async with self._db.begin() as conn:
            await execute(conn, "CREATE SCHEMA synthetic_restart")
            await execute(
                conn,
                """
                CREATE TABLE synthetic_restart.services (
                    workspace_id text, customer_id text, service_id text,
                    restart_allowed boolean NOT NULL,
                    boot_generation bigint NOT NULL CHECK (boot_generation >= 0),
                    PRIMARY KEY (workspace_id, customer_id, service_id)
                )
                """,
            )
            await execute(
                conn,
                "CREATE TABLE synthetic_restart.arrivals (operation_id uuid NOT NULL)",
            )
            await execute(
                conn,
                """
                CREATE TABLE synthetic_restart.outcomes (
                    operation_id uuid PRIMARY KEY,
                    binding bytea NOT NULL CHECK (octet_length(binding)=32),
                    workspace_id text NOT NULL, customer_id text NOT NULL, service_id text NOT NULL,
                    sender_worker uuid NOT NULL, sender_generation bigint NOT NULL CHECK (sender_generation>0),
                    prior_boot_generation bigint CHECK (prior_boot_generation>=0),
                    boot_generation bigint CHECK (boot_generation>0),
                    CHECK ((prior_boot_generation IS NULL)=(boot_generation IS NULL)),
                    CHECK (prior_boot_generation IS NULL OR boot_generation-prior_boot_generation=1)
                )
                """,
            )

    async def close(self) -> None:
        async with self._db.begin() as conn:
            await execute(conn, "DROP SCHEMA synthetic_restart CASCADE")

    async def invoke(self, intent: RestartIntent) -> None:
        operation_id = intent.owner.execution_id
        async with self._db.begin() as conn:
            await execute(
                conn,
                "INSERT INTO synthetic_restart.arrivals VALUES (:id)",
                id=operation_id,
            )
        async with self._db.begin() as conn:
            await execute(conn, "SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            await execute(conn, "SET LOCAL search_path = pg_catalog, public, pg_temp")
            await lock_durable_coordination(conn)
            if not await rows(
                conn,
                "SELECT id FROM public.executions WHERE id=:id FOR UPDATE",
                id=operation_id,
            ):
                raise CommandStoreError("SYNTHETIC_RESTART_NOT_AUTHORIZED")
            attempts = await rows(
                conn,
                """
                SELECT c.state, c.lease_owner, a.outcome
                FROM public.execution_commands c
                JOIN public.execution_attempts a
                  ON a.execution_id=c.execution_id AND a.generation=c.generation
                WHERE c.execution_id=:execution_id AND c.tool_id=:tool
                  AND a.generation=:generation AND a.worker_id=:worker_id
                  AND a.kind='SEND' AND a.send_marked_at IS NOT NULL
                FOR UPDATE OF c, a
                """,
                tool=TOOL,
                **intent.owner.model_dump(),
            )
            if (
                len(attempts) != 1
                or intent.request.customer_id != intent.scope.customer_id
            ):
                raise CommandStoreError("SYNTHETIC_RESTART_NOT_AUTHORIZED")

            # Keep both evidence rows and their guards fixed through the effect.
            # This lock also excludes audit inserts by trusted fixture writers.
            await execute(
                conn, "LOCK TABLE public.audit_events IN SHARE ROW EXCLUSIVE MODE"
            )
            await ExecutionWorkerStore._lock_synthetic_audit_guards(conn)
            await ExecutionWorkerStore._verify_marker_contract(
                conn, intent.owner, intent.binding, CONTRACT
            )
            consumed = await rows(
                conn,
                """
                SELECT actor FROM public.audit_events
                WHERE resource_id=:id AND action='SYNTHETIC_INVOCATION_CONSUMED'
                """,
                id=operation_id,
            )
            if consumed != [{"actor": str(intent.owner.worker_id)}]:
                raise CommandStoreError("SYNTHETIC_RESTART_NOT_AUTHORIZED")

            [attempt] = attempts
            if attempt["state"] in ("SUCCEEDED", "FAILED", "UNKNOWN") and (
                attempt["lease_owner"] is None
                and attempt["outcome"] == attempt["state"]
            ):
                # A matching delivery after terminal classification can never
                # reserve an operation or create a new effect.
                return
            if (
                attempt["state"] != "DISPATCHING"
                or attempt["lease_owner"] != intent.owner.worker_id
                or attempt["outcome"] is not None
            ):
                raise CommandStoreError("SYNTHETIC_RESTART_NOT_AUTHORIZED")

            reserved = await rows(
                conn,
                """
                INSERT INTO synthetic_restart.outcomes
                    (operation_id,binding,workspace_id,customer_id,service_id,sender_worker,sender_generation)
                VALUES (:id,:binding,:workspace,:customer,:service,:worker,:generation)
                ON CONFLICT (operation_id) DO NOTHING RETURNING operation_id
                """,
                id=operation_id,
                binding=intent.binding,
                workspace=intent.scope.workspace_id,
                customer=intent.scope.customer_id,
                service=intent.request.service_id,
                worker=intent.owner.worker_id,
                generation=intent.owner.generation,
            )
            if not reserved:
                return
            restarted = await rows(
                conn,
                """
                UPDATE synthetic_restart.services
                SET boot_generation=boot_generation+1
                WHERE workspace_id=:workspace AND customer_id=:customer AND service_id=:service
                  AND restart_allowed
                RETURNING boot_generation,boot_generation-1 AS prior_boot_generation
                """,
                workspace=intent.scope.workspace_id,
                customer=intent.scope.customer_id,
                service=intent.request.service_id,
            )
            if restarted:
                await execute(
                    conn,
                    """
                    UPDATE synthetic_restart.outcomes SET boot_generation=:boot_generation,
                        prior_boot_generation=:prior_boot_generation WHERE operation_id=:id
                    """,
                    id=operation_id,
                    **restarted[0],
                )

    async def evidence(self, intent: RestartIntent) -> SyntheticOutcome | None:
        return await read_restart_evidence(self._db, self._key, intent)


class RestartFlow(SyntheticLookupFlow[RestartIntent]):
    def __init__(
        self,
        approvals: ExecutionApprovals,
        ownership: ExecutionWorkerStore,
        ledger: RestartLedger,
    ):
        super().__init__(
            approvals,
            ownership,
            ledger,
            tool=TOOL,
            build_intent=restart_intent,
            evidence_contract=CONTRACT,
        )
