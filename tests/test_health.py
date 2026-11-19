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


def test_metrics_use_static_route_names(client):
    client.get("/healthz")
    client.get("/sensitive-query?secret=fake_metric_secret")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert 'route="/healthz",status="200"' in response.text
    assert 'route="unmatched",status="404"' in response.text
    assert "fake_metric_secret" not in response.text
    assert "sensitive-query" not in response.text


def test_otlp_exports_correlated_spans_without_raw_paths():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    exports = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            exports.append(ExportTraceServiceRequest.FromString(payload))
            self.send_response(200)
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", 0), Collector) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            config = Settings(
                database_url="postgresql+asyncpg://user:fake_password@127.0.0.1:1/toolmesh",
                redis_url="redis://127.0.0.1:1/0",
                otlp_traces_endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
            )
            with TestClient(create_app(config)) as client:
                response = client.get("/private-missing-path?token=sample-query-value")
        finally:
            server.shutdown()
            worker.join()

    spans = [
        span
        for batch in exports
        for resource in batch.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
        if span.name == "http.request"
    ]
    assert len(spans) == 1
    attributes = {item.key: item.value.string_value for item in spans[0].attributes}
    assert attributes["http.route"] == "unmatched"
    assert attributes["toolmesh.request_id"] == response.headers["X-Request-ID"]
    assert b"sample-query-value" not in spans[0].SerializeToString()
    assert b"private-missing-path" not in spans[0].SerializeToString()
