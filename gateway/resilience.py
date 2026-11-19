import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from fastapi import HTTPException


def transient(exc: Exception) -> bool:
    if isinstance(exc, ExceptionGroup):
        return all(transient(item) for item in exc.exceptions)
    if isinstance(exc, RuntimeError) and isinstance(exc.__cause__, Exception):
        return transient(exc.__cause__)
    if isinstance(exc, (TimeoutError, httpx.NetworkError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 502, 503, 504}
    return isinstance(exc, HTTPException) and exc.detail in {
        "UPSTREAM_TIMEOUT",
        "UPSTREAM_UNAVAILABLE",
    }


@dataclass
class Circuit:
    failures: int = 0
    open_until: float = 0
    probing: bool = False


class ReadDispatch:
    def __init__(self, record: Callable[[str, str], None]):
        self.circuits: dict[str, Circuit] = {}
        self.record = record

    async def call(self, service: str, invoke: Callable[[], Awaitable[dict]]) -> dict:
        circuit = self.circuits.setdefault(service, Circuit())
        if circuit.open_until > time.monotonic() or circuit.probing:
            self.record(service, "rejected")
            raise HTTPException(503, "UPSTREAM_CIRCUIT_OPEN")
        probe = bool(circuit.open_until)
        if probe:
            circuit.probing = True
        try:
            async with asyncio.timeout(7):
                for attempt in range(3):
                    try:
                        result = await invoke()
                    except Exception as exc:
                        if not transient(exc):
                            raise
                        if probe or attempt == 2:
                            raise
                        self.record(service, "retry")
                        await asyncio.sleep(random.uniform(0.05, 0.15) * 2**attempt)
                    else:
                        if probe or not circuit.open_until:
                            circuit.failures = 0
                            circuit.open_until = 0
                        return result
        except Exception as exc:
            if circuit.open_until and not probe:
                raise
            if transient(exc):
                circuit.failures += 1
                if probe or circuit.failures >= 3:
                    circuit.open_until = time.monotonic() + 15
                    self.record(service, "opened")
            else:
                circuit.failures = 0
                circuit.open_until = 0
            raise
        finally:
            if probe:
                circuit.probing = False
        raise RuntimeError("Read dispatch exhausted")
