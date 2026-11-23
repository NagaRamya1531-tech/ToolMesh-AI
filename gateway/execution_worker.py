"""Standalone pre-send ownership exercise for disposable PostgreSQL.

On SIGTERM or SIGINT, process teardown has the configured drain budget plus
250 ms for driver cancellation and pool cleanup. At that deadline, process
exit closes any remaining sockets without requiring a PostgreSQL response.
"""

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import timedelta
from functools import partial
from random import uniform
from threading import Timer
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from gateway.config import SandboxWorkerSettings
from gateway.execution_approval import ExecutionApprovals
from gateway.execution_contracts import ExecutionLease
from gateway.execution_crypto import (
    CommandDigests,
    PayloadProtection,
    SandboxWrappingKeys,
)
from gateway.execution_store import CommandStoreError
from gateway.sandbox_refund_evidence import read_refund_evidence
from gateway.sandbox_refund_reconciliation import RefundReconciler
from gateway.sandbox_refund_store import SandboxRefundStore
from gateway.sandbox_worker_store import SUPPORTED_REVISION, SandboxWorkerStore

logger = logging.getLogger(__name__)
_BLOCKED = {
    "DURABLE_ADMISSION_DISABLED",
    "EXECUTION_COORDINATION_UNAVAILABLE",
    "EXECUTION_OWNERSHIP_UNAVAILABLE",
}
UNCONFIRMED_OWNERSHIP_EXIT = 3
# Allow responsive drivers to finish cancellation after cooperative draining.
SHUTDOWN_CLEANUP_SECONDS = 0.25


class OwnershipUnconfirmed(CommandStoreError):
    """An ownership mutation may have committed without a response."""


class ShutdownDeadline:
    """Process-level backstop for driver cancellation and pool disposal."""

    def __init__(self) -> None:
        self.awaiting_ownership: str | None = None
        self.awaiting_verification: str | None = None
        self._timer: Timer | None = None

    def arm(self, drain_seconds: float) -> None:
        if self._timer is None:
            self._timer = Timer(drain_seconds + SHUTDOWN_CLEANUP_SECONDS, self._exit)
            self._timer.daemon = True
            self._timer.start()

    def disarm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    @contextmanager
    def verification_mutation(self, operation: str) -> Iterator[None]:
        self.awaiting_verification = operation
        try:
            yield
        except asyncio.CancelledError:
            raise OwnershipUnconfirmed(operation) from None
        except CommandStoreError as error:
            if str(error) == "EXECUTION_OWNERSHIP_UNAVAILABLE":
                raise OwnershipUnconfirmed(operation) from None
            self.awaiting_verification = None
            raise
        else:
            self.awaiting_verification = None

    def _exit(self) -> None:
        # The OS discards the pool and closes its sockets. Do not wait on the
        # database, run async teardown, or issue another ownership operation.
        os._exit(
            UNCONFIRMED_OWNERSHIP_EXIT
            if self.awaiting_ownership or self.awaiting_verification
            else 0
        )


async def _stopping(stop: asyncio.Event, delay: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), delay)
    except TimeoutError:
        return False
    return True


async def exercise(
    settings: SandboxWorkerSettings,
    store: SandboxWorkerStore,
    stop: asyncio.Event,
    *,
    shutdown: ShutdownDeadline | None = None,
    send_checked: asyncio.Event | None = None,
) -> None:
    if shutdown is None:
        shutdown = ShutdownDeadline()
    if stop.is_set():
        return
    await store.validate_schema()
    if stop.is_set():
        return
    worker = await store.register()
    logger.info("worker_ready worker_id=%s schema=%s", worker, SUPPORTED_REVISION)
    lease: ExecutionLease | None = None
    delay = settings.poll_seconds
    try:
        while not stop.is_set() and lease is None:
            try:
                # Recheck after every poll, including after reconnecting from an outage.
                await store.validate_schema()
                if stop.is_set():
                    break
                recovered = await store.recover_next(worker)
                if recovered:
                    logger.info(
                        "ownership_recovered state=%s generation=%s",
                        recovered.state,
                        recovered.generation,
                    )
                if stop.is_set():
                    break
                shutdown.awaiting_ownership = "claim"
                lease = await store.claim(worker)
                shutdown.awaiting_ownership = None
                if send_checked is not None:
                    send_checked.set()
            except CommandStoreError as error:
                if (
                    shutdown.awaiting_ownership == "claim"
                    and str(error) == "EXECUTION_OWNERSHIP_UNAVAILABLE"
                ):
                    logger.info("ownership_claim_unconfirmed")
                    raise OwnershipUnconfirmed("claim") from None
                shutdown.awaiting_ownership = None
                if str(error) not in _BLOCKED:
                    raise
                logger.info("ownership_paused reason=%s", error)
            if lease is None and not stop.is_set():
                await _stopping(stop, uniform(delay / 2, delay))
                delay = min(settings.max_poll_seconds, 2 * delay)
        if lease is None:
            return
        logger.info(
            "ownership_claimed execution_id=%s generation=%s",
            lease.owner.execution_id,
            lease.owner.generation,
        )
        while not await _stopping(stop, settings.renew_seconds):
            try:
                shutdown.awaiting_ownership = "renew"
                renewed = await store.renew(lease.owner)
                shutdown.awaiting_ownership = None
            except CommandStoreError as error:
                logger.info("ownership_unconfirmed reason=%s", error)
                lease = None
                raise OwnershipUnconfirmed("renew") from None
            if renewed is None:
                logger.info("ownership_lost")
                lease = None
                return
            lease = renewed
            logger.info("ownership_renewed generation=%s", lease.owner.generation)
    except asyncio.CancelledError:
        # The supervisor exhausted its drain budget. Leave any lease to the
        # database rather than starting another write during cancellation.
        lease = None
        if shutdown.awaiting_ownership is not None:
            raise OwnershipUnconfirmed(shutdown.awaiting_ownership) from None
        raise
    finally:
        if lease is not None and stop.is_set():
            try:
                async with asyncio.timeout(settings.drain_seconds):
                    released = await store.release(lease.owner)
                logger.info(
                    "ownership_release result=%s", "released" if released else "fenced"
                )
            except (CommandStoreError, TimeoutError):
                logger.info("ownership_release result=unconfirmed")


async def _poll_refunds(
    settings: SandboxWorkerSettings,
    store: SandboxRefundStore,
    reconciler: RefundReconciler,
    stop: asyncio.Event,
    shutdown: ShutdownDeadline,
    send_checked: asyncio.Event,
) -> None:
    # SEND gets the initial poll. Afterwards each lane has its own connection
    # checkout and cadence, so a held SEND lease cannot monopolize verification.
    await send_checked.wait()
    verifier = uuid4()
    delay = settings.poll_seconds
    while not stop.is_set():
        try:
            for execution_id in await store.discover_due_synthetic_refunds(limit=1):
                result = await reconciler.run_once(
                    execution_id,
                    verifier,
                    stopping=stop.is_set,
                    mutation=shutdown.verification_mutation,
                )
                if result is not None:
                    logger.info(
                        "refund_verified execution_id=%s state=%s", execution_id, result
                    )
                    delay = settings.poll_seconds
        except OwnershipUnconfirmed:
            raise
        except CommandStoreError as error:
            if str(error) not in _BLOCKED | {
                "REFUND_VERIFICATION_DISCOVERY_UNAVAILABLE"
            }:
                raise
            logger.info("refund_verification_paused reason=%s", error)
        await _stopping(stop, uniform(delay / 2, delay))
        delay = min(settings.max_poll_seconds, 2 * delay)


async def _exercise_with_refunds(
    settings: SandboxWorkerSettings,
    store: SandboxWorkerStore,
    stop: asyncio.Event,
    shutdown: ShutdownDeadline,
    verification_store: SandboxRefundStore,
    reconciler: RefundReconciler,
) -> None:
    if stop.is_set():
        return
    await verification_store.validate_access()
    send_checked = asyncio.Event()
    tasks = (
        asyncio.create_task(
            exercise(
                settings, store, stop, shutdown=shutdown, send_checked=send_checked
            )
        ),
        asyncio.create_task(
            _poll_refunds(
                settings, verification_store, reconciler, stop, shutdown, send_checked
            )
        ),
    )
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # A normal signal lets both lanes drain. A fatal error or SEND lease loss
        # stops the sibling and leaves any interrupted ownership to its fence.
        if stop.is_set():
            send_checked.set()
            await asyncio.wait(tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, OwnershipUnconfirmed):
                raise result
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                raise result


def _refund_verification(
    db: AsyncEngine, settings: SandboxWorkerSettings
) -> tuple[SandboxRefundStore, RefundReconciler] | None:
    if settings.refund_verification_keys is None:
        return None
    parts = settings.refund_verification_keys.get_secret_value().split(":")
    if len(parts) != 4 or any(len(part) != 64 for part in parts):
        raise ValueError("invalid sandbox refund verification keys")
    wrapping, lookup, intent, evidence = (bytes.fromhex(part) for part in parts)
    payloads = PayloadProtection(SandboxWrappingKeys({1: wrapping}, 1))
    approvals = ExecutionApprovals(
        db, payloads, CommandDigests(lookup, intent), timedelta(minutes=3)
    )
    store = SandboxRefundStore(
        db, approvals, timedelta(seconds=settings.lease_seconds), evidence
    )
    return store, RefundReconciler(
        approvals, store, partial(read_refund_evidence, db, evidence)
    )


async def run(settings: SandboxWorkerSettings, shutdown: ShutdownDeadline) -> None:
    if not settings.enabled:
        raise ValueError("sandbox worker must be explicitly enabled")
    if (
        settings.renew_seconds >= settings.lease_seconds
        or settings.poll_seconds > settings.max_poll_seconds
    ):
        raise ValueError("invalid sandbox worker timing")
    url = make_url(settings.database_url.get_secret_value())
    if url.drivername != "postgresql+asyncpg" or url.username in {
        None,
        "toolmesh_app",
        "toolmesh_migrator",
    }:
        raise ValueError("sandbox worker requires a dedicated PostgreSQL login")
    db = create_async_engine(
        url,
        hide_parameters=True,
        pool_size=2 if settings.refund_verification_keys is not None else 1,
        max_overflow=0,
        pool_timeout=2,
        pool_pre_ping=True,
        connect_args={"timeout": 2},
    )
    try:
        refunds = _refund_verification(db, settings)
    except ValueError:
        await db.dispose()
        raise
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def request_stop() -> None:
        shutdown.arm(settings.drain_seconds)
        stop.set()

    def handle_signal(_number: int, _frame: object) -> None:
        if not loop.is_closed():
            loop.call_soon_threadsafe(request_stop)

    # This standalone process owns these handlers until exit. Event-loop
    # handlers would restore SIGTERM's default action before main selects its status.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handle_signal)
    store = SandboxWorkerStore(db, timedelta(seconds=settings.lease_seconds))
    task = asyncio.create_task(
        exercise(settings, store, stop, shutdown=shutdown)
        if refunds is None
        else _exercise_with_refunds(settings, store, stop, shutdown, *refunds)
    )
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            (task, waiter), return_when=asyncio.FIRST_COMPLETED
        )
        if task not in done:
            done, _ = await asyncio.wait((task,), timeout=settings.drain_seconds)
            if task not in done:
                logger.info("worker_drain_deadline")
                task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    finally:
        primary_error = sys.exception()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        try:
            await db.dispose()
        except Exception as error:
            logger.error(
                "worker_pool_cleanup_failed error_type=%s", type(error).__name__
            )
            if primary_error is None:
                raise
        except asyncio.CancelledError:
            if not isinstance(primary_error, OwnershipUnconfirmed):
                raise
            logger.error("worker_pool_cleanup_failed error_type=CancelledError")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    shutdown = ShutdownDeadline()
    try:
        asyncio.run(run(SandboxWorkerSettings(), shutdown))  # type: ignore[call-arg]
    except (ValidationError, ArgumentError, ValueError):
        logger.error("worker_configuration_rejected")
        return 2
    except OwnershipUnconfirmed as error:
        logger.error("worker_ownership_unconfirmed operation=%s", error)
        return UNCONFIRMED_OWNERSHIP_EXIT
    except CommandStoreError as error:
        logger.error(
            "worker_stopped reason=%s supported_schema=%s", error, SUPPORTED_REVISION
        )
        return 2
    finally:
        shutdown.disarm()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
