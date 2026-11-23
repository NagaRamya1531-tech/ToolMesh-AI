from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.execution_contracts import ExecutionLease, ExecutionOwner
from gateway.execution_worker_store import ExecutionWorkerStore


@pytest.mark.parametrize("generation", [0, -1, True, "1", 2**63])
def test_owner_requires_positive_bigint_generation(generation):
    with pytest.raises(ValidationError):
        ExecutionOwner(execution_id=uuid4(), worker_id=uuid4(), generation=generation)


def test_lease_requires_aware_database_time():
    owner = ExecutionOwner(execution_id=uuid4(), worker_id=uuid4(), generation=1)
    with pytest.raises(ValidationError, match="timezone"):
        ExecutionLease(owner=owner, expires_at=datetime(2026, 1, 1))


def test_store_requires_positive_duration_and_hidden_parameters():
    with pytest.raises(ValueError, match="hidden SQL parameters"):
        ExecutionWorkerStore(create_async_engine("postgresql+asyncpg://"), timedelta(1))
    db = create_async_engine("postgresql+asyncpg://", hide_parameters=True)
    with pytest.raises(ValueError, match="positive"):
        ExecutionWorkerStore(db, timedelta())


def test_worker_shutdown_drains_acknowledged_claim():
    import asyncio
    from datetime import UTC
    from types import SimpleNamespace

    from gateway.config import SandboxWorkerSettings
    from gateway.execution_worker import exercise

    worker = uuid4()
    lease = ExecutionLease(
        owner=ExecutionOwner(execution_id=uuid4(), worker_id=worker, generation=1),
        expires_at=datetime.now(UTC) + timedelta(seconds=20),
    )
    settings = SandboxWorkerSettings(
        database_url="postgresql+asyncpg://test@localhost/db"
    )

    async def check():
        stop = asyncio.Event()
        operations = []

        async def value(name, result=None):
            operations.append(name)
            return result

        async def claim(worker_id):
            assert worker_id == worker
            stop.set()
            return await value("claim", lease)

        async def release(owner):
            assert owner == lease.owner
            await value("release")
            return True

        store = SimpleNamespace(
            validate_schema=lambda: value("schema"),
            register=lambda: value("register", worker),
            recover_next=lambda _: value("recover"),
            claim=claim,
            release=release,
        )
        await exercise(settings, store, stop)
        assert operations == [
            "schema",
            "register",
            "schema",
            "recover",
            "claim",
            "release",
        ]

    asyncio.run(check())


def test_worker_stops_after_unconfirmed_claim():
    import asyncio
    from types import SimpleNamespace

    from gateway.config import SandboxWorkerSettings
    from gateway.execution_store import CommandStoreError
    from gateway.execution_worker import OwnershipUnconfirmed, exercise

    async def check():
        calls = []

        async def no_op(*_):
            return None

        async def register():
            return uuid4()

        async def unconfirmed(*_):
            calls.append("claim")
            raise CommandStoreError("EXECUTION_OWNERSHIP_UNAVAILABLE")

        store = SimpleNamespace(
            validate_schema=no_op,
            register=register,
            recover_next=no_op,
            claim=unconfirmed,
        )
        with pytest.raises(OwnershipUnconfirmed, match="^claim$"):
            await asyncio.wait_for(
                exercise(
                    SandboxWorkerSettings(
                        database_url="postgresql+asyncpg://test@localhost/db"
                    ),
                    store,
                    asyncio.Event(),
                ),
                timeout=1,
            )
        assert calls == ["claim"]

    asyncio.run(check())
