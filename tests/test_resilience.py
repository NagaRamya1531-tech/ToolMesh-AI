import asyncio

import httpx
import pytest
from fastapi import HTTPException

from gateway import resilience
from gateway.resilience import ReadDispatch


def test_backoff_circuit_and_single_half_open_probe(monkeypatch):
    events = []
    clock = [100.0]
    monkeypatch.setattr(
        resilience, "time", type("Clock", (), {"monotonic": lambda: clock[0]})
    )
    dispatch = ReadDispatch(lambda service, event: events.append((service, event)))

    async def unavailable():
        raise httpx.ConnectError("test")

    async def scenario():
        for _ in range(3):
            with pytest.raises(httpx.ConnectError):
                await dispatch.call("payments", unavailable)
        with pytest.raises(HTTPException, match="UPSTREAM_CIRCUIT_OPEN"):
            await dispatch.call("payments", unavailable)
        clock[0] += 15
        entered = asyncio.Event()
        release = asyncio.Event()

        async def recovered():
            entered.set()
            await release.wait()
            return {"ok": True}

        probe = asyncio.create_task(dispatch.call("payments", recovered))
        await entered.wait()
        with pytest.raises(HTTPException, match="UPSTREAM_CIRCUIT_OPEN"):
            await dispatch.call("payments", unavailable)
        release.set()
        assert await probe == {"ok": True}
        assert await dispatch.call("payments", recovered) == {"ok": True}

    asyncio.run(scenario())
    assert events == [("payments", "retry")] * 6 + [
        ("payments", "opened"),
        ("payments", "rejected"),
        ("payments", "rejected"),
    ]


def test_deterministic_errors_do_not_retry():
    def unexpected(*_):
        pytest.fail("Deterministic errors must not be retried")

    async def no_record():
        raise HTTPException(404, "RECORD_NOT_AVAILABLE")

    with pytest.raises(HTTPException, match="RECORD_NOT_AVAILABLE"):
        asyncio.run(ReadDispatch(unexpected).call("payments", no_record))
