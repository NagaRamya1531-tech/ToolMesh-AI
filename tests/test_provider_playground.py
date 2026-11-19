from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from gateway import provider

PATH = "/api/v1/playground/provider"


@pytest.fixture
def playground(monkeypatch):
    principal = SimpleNamespace(customer="C-102")
    identity = SimpleNamespace(principal=AsyncMock(return_value=principal))
    runtime = SimpleNamespace(
        tools=AsyncMock(
            return_value=[
                {
                    "id": operation,
                    "version": 2,
                    "contract": {
                        "risk": risk,
                        "description": operation,
                        "input_schema": {"type": "object"},
                    },
                }
                for operation, risk in (
                    ("payments.get_transaction", "read"),
                    ("payments.issue_refund", "approval"),
                    ("support.create_ticket", "write"),
                )
            ]
        ),
        quota=AsyncMock(),
        telemetry=SimpleNamespace(span=lambda _: nullcontext()),
        invoke=AsyncMock(return_value={"status": "SUCCEEDED", "id": "fixture"}),
    )
    app = FastAPI()
    app.include_router(
        provider.provider_api(identity, runtime, SecretStr("local-fixture-key"))
    )
    choose = AsyncMock()
    monkeypatch.setattr(provider, "choose", choose)
    return TestClient(app), identity, runtime, choose, principal


def test_provider_only_receives_reads_and_runtime_owns_customer(playground):
    client, identity, runtime, choose, principal = playground
    choose.return_value = (
        provider.Selection(
            tool="payments.get_transaction",
            arguments_json='{"transaction_id":"TX-102-A"}',
        ),
        {"input_tokens": 100, "output_tokens": 10},
    )
    config = client.get(PATH)
    assert config.json() == {"available": True, "model": provider.MODEL}
    reply = client.post(PATH, json={"prompt": "Inspect the transaction"})
    assert reply.json()["status"] == "SUCCEEDED"
    assert reply.json()["usage"]["input_tokens"] == 100
    assert [row["id"] for row in choose.await_args.args[2]] == [
        "payments.get_transaction"
    ]
    runtime.quota.assert_awaited_once_with(principal, "provider.openai")
    runtime.invoke.assert_awaited_once_with(
        principal,
        "payments.get_transaction",
        {"transaction_id": "TX-102-A", "customer_id": "C-102"},
        2,
        read_only=True,
    )
    assert identity.principal.await_count == 3


@pytest.mark.parametrize(
    ("tool", "arguments", "status"),
    [
        ("payments.issue_refund", "{}", "INVALID_SELECTION"),
        ("payments.get_transaction", '{"customer_id":"C-104"}', "INVALID_ARGUMENTS"),
        ("payments.get_transaction", "[]", "INVALID_ARGUMENTS"),
        ("NONE", "{}", "NO_SELECTION"),
    ],
)
def test_invalid_selections_stop_before_execution(playground, tool, arguments, status):
    client, _, runtime, choose, _ = playground
    choose.return_value = provider.Selection(tool=tool, arguments_json=arguments), {}
    reply = client.post(PATH, json={"prompt": "Read sandbox records"})
    assert reply.json()["status"] == status
    runtime.invoke.assert_not_awaited()


def test_quota_failure_prevents_billable_request(playground):
    client, _, runtime, choose, _ = playground
    runtime.quota.side_effect = HTTPException(503, "QUOTA_UNAVAILABLE")
    response = client.post(PATH, json={"prompt": "Read sandbox records"})
    assert response.status_code == 503
    choose.assert_not_awaited()
    runtime.invoke.assert_not_awaited()
