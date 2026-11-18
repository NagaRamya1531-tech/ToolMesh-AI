from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gateway.app import create_app
from gateway.config import Settings


@pytest.fixture
def client():
    config = Settings(
        database_url="postgresql+asyncpg://user:fake_password@127.0.0.1:1/toolmesh",
        redis_url="redis://127.0.0.1:1/0",
        probe_timeout=0.3,
    )
    with TestClient(create_app(config)) as http:
        yield http


def test_liveness_does_not_require_dependencies(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert response.headers["Cache-Control"] == "no-store"
    assert UUID(response.headers["X-Request-ID"])


def test_unavailable_dependencies_return_safe_readiness(client):
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unready"
    assert body["checks"] == {"postgres": "unavailable", "redis": "unavailable"}
    assert "fake_password" not in response.text


def test_error_envelope_has_request_id(client):
    response = client.get("/absent")
    assert response.status_code == 404
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_timeout_must_be_bounded():
    with pytest.raises(ValidationError):
        Settings(database_url="url", redis_url="url", probe_timeout=0)


def test_body_limit_rejects_oversized_content(client):
    response = client.post("/api/v1/auth/login", content=b"x" * 32769)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


def test_invalid_login_payload_does_not_echo_password(client):
    response = client.post("/api/v1/auth/login", json={"password": "value-to-redact"})
    assert response.status_code == 422
    assert "value-to-redact" not in response.text
