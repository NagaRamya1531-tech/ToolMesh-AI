"""Exercise the deployed API with actual Postgres and Redis."""

import os

import httpx2
import pytest


@pytest.mark.skipif(
    not os.getenv("TOOLMESH_TEST_API"), reason="requires running Compose API"
)
def test_compose_readiness():
    origin = os.environ["TOOLMESH_TEST_API"]
    health = httpx2.get(f"{origin}/healthz")
    assert health.status_code == 200
    readiness = httpx2.get(f"{origin}/readyz")
    assert readiness.status_code == 200
    assert readiness.json()["checks"] == {"postgres": "available", "redis": "available"}
