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
from contextlib import suppress
from datetime import timedelta
from random import uniform
from threading import Timer

from pydantic import ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.config import SandboxWorkerSettings
from gateway.execution_contracts import ExecutionLease
from gateway.execution_store import CommandStoreError
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
        self._timer: Timer | None = None

    def arm(self, drain_seconds: float) -> None:
        if self._timer is None:
            self._timer = Timer(drain_seconds + SHUTDOWN_CLEANUP_SECONDS, self._exit)
            self._timer.daemon = True
            self._timer.start()

    def disarm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def _exit(self) -> None:
        # The OS discards the pool and closes its sockets. Do not wait on the
        # database, run async teardown, or issue another ownership operation.
        os._exit(UNCONFIRMED_OWNERSHIP_EXIT if self.awaiting_ownership else 0)


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
            if lease is None:
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
        pool_size=1,
        max_overflow=0,
        pool_timeout=2,
        pool_pre_ping=True,
        connect_args={"timeout": 2},
    )
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
    task = asyncio.create_task(
        exercise(
            settings,
            SandboxWorkerStore(db, timedelta(seconds=settings.lease_seconds)),
            stop,
            shutdown=shutdown,
        )
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
