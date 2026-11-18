"""Exercise the MCP and REST interfaces against the deployed sandbox."""

import asyncio
import os

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

ORIGIN = os.getenv("TOOLMESH_TEST_API")
pytestmark = pytest.mark.skipif(
    not ORIGIN, reason="requires Compose and sandbox credentials"
)

PASSWORDS = {
    "platform-admin": ("TOOLMESH_ADMIN_PASSWORD", "local_admin_password"),
    "support-agent": ("TOOLMESH_SUPPORT_PASSWORD", "local_support_password"),
    "payments-agent": ("TOOLMESH_PAYMENTS_PASSWORD", "local_payments_password"),
}


async def login(api, principal):
    env, default = PASSWORDS[principal]
    response = await api.post(
        "/api/v1/auth/login",
        json={"username": principal, "password": os.getenv(env, default)},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_runtime_mcp_and_persisted_domain_records():
    async def scenario():
        async with httpx.AsyncClient(base_url=ORIGIN, timeout=10) as api:
            admin = await login(api, "platform-admin")
            support = await login(api, "support-agent")
            payments = await login(api, "payments-agent")
            assert (await api.get("/api/v1/services")).status_code == 401
            denied = await api.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/list",
                    "id": 1,
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert denied.status_code == 401
            async with httpx.AsyncClient(base_url=f"{ORIGIN}/api/v1", timeout=10) as v1:
                await exercise(v1, admin, support, payments)

    async def exercise(api, admin, support, payments):
        tool_ids = [
            "payments.check_duplicate_payment",
            "payments.list_transactions",
            "payments.get_transaction",
            "support.create_ticket",
            "support.search_tickets",
            "support.get_ticket",
        ]
        publication = {"version": 1, "reason": "Verify sandbox contract"}
        forbidden = await api.post(
            f"/tools/{tool_ids[0]}/publish", headers=payments, json=publication
        )
        assert forbidden.status_code == 403
        for tool in tool_ids:
            response = await api.post(
                f"/tools/{tool}/publish", headers=admin, json=publication
            )
            assert response.status_code == 200, response.text

        async with Client(
            f"{ORIGIN}/mcp/", auth=support["Authorization"].split()[1], timeout=8
        ) as client:
            assert {tool.name for tool in await client.list_tools()} == {
                "discover_server",
                "discover_tools",
                "get_tool_schema",
                "invoke_tool",
            }
            servers = await client.call_tool("discover_server", {"query": "pay"})
            assert [server["id"] for server in servers.data["servers"]] == ["payments"]
            tools = await client.call_tool(
                "discover_tools", {"server_id": "payments", "query": "duplicate"}
            )
            assert [tool["id"] for tool in tools.data["tools"]] == [tool_ids[0]]
            assert "input_schema" not in tools.data["tools"][0]
            contract = await client.call_tool(
                "get_tool_schema", {"tool_id": tool_ids[0]}
            )
            assert contract.data["version"] == 1
            for order, expected in (("O-102", True), ("O-103", False)):
                check = await client.call_tool(
                    "invoke_tool",
                    {
                        "tool_id": tool_ids[0],
                        "arguments": {"customer_id": "C-102", "order_id": order},
                    },
                )
                assert check.data["status"] == "SUCCEEDED", check.data
                assert check.data["result"]["duplicate_capture_detected"] is expected
            assert check.data["result"]["groups"] == []

            ticket = await client.call_tool(
                "invoke_tool",
                {
                    "tool_id": "support.create_ticket",
                    "arguments": {
                        "customer_id": "C-102",
                        "order_id": "O-102",
                        "subject": "Duplicate capture investigation",
                    },
                },
            )
            assert ticket.data["status"] == "SUCCEEDED", ticket.data
            ticket_id = ticket.data["result"]["id"]
            trace_id = ticket.data["id"]

            for tool, args in (
                (
                    "support.get_ticket",
                    {"customer_id": "C-102", "ticket_id": ticket_id},
                ),
                ("support.search_tickets", {"customer_id": "C-102"}),
                (
                    "payments.get_transaction",
                    {"customer_id": "C-102", "transaction_id": "TX-102-A"},
                ),
                ("payments.list_transactions", {"customer_id": "C-102"}),
            ):
                result = await client.call_tool(
                    "invoke_tool", {"tool_id": tool, "arguments": args}
                )
                assert result.data["status"] == "SUCCEEDED", result.data
                if tool == "support.get_ticket":
                    assert result.data["result"]["id"] == ticket_id
                if tool == "support.search_tickets":
                    assert ticket_id in [
                        item["id"] for item in result.data["result"]["tickets"]
                    ]

        async with Client(
            f"{ORIGIN}/mcp/", auth=payments["Authorization"].split()[1], timeout=8
        ) as limited:
            discovered = await limited.call_tool(
                "discover_tools", {"server_id": "support"}
            )
            assert discovered.data["tools"] == []
            with pytest.raises(ToolError, match="TOOL_NOT_AVAILABLE"):
                await limited.call_tool(
                    "get_tool_schema", {"tool_id": "support.create_ticket"}
                )

        invalid = await api.post(
            "/executions",
            headers=payments,
            json={
                "tool_id": "payments.get_transaction",
                "arguments": {
                    "customer_id": "C-102",
                    "transaction_id": "TX-102-A",
                    "identity": "platform-admin",
                },
            },
        )
        assert invalid.json()["error_code"] == "INVALID_ARGUMENT"

        projected = await api.post(
            "/executions",
            headers=payments,
            json={
                "tool_id": "payments.get_transaction",
                "arguments": {"customer_id": "C-102", "transaction_id": "TX-102-A"},
                "fields": ["amount_minor"],
            },
        )
        assert projected.json()["result"] == {"amount_minor": 4550}
        invalid_projection = await api.post(
            "/executions",
            headers=payments,
            json={
                "tool_id": "payments.get_transaction",
                "arguments": {"customer_id": "C-102", "transaction_id": "TX-102-A"},
                "fields": ["password"],
            },
        )
        assert invalid_projection.json()["error_code"] == "INVALID_ARGUMENT"

        scoped = await api.post(
            "/executions",
            headers=payments,
            json={
                "tool_id": "payments.get_transaction",
                "arguments": {"customer_id": "C-104", "transaction_id": "TX-104-A"},
            },
        )
        assert scoped.json()["error_code"] == "TOOL_NOT_AVAILABLE"
        assert scoped.json()["status"] == "DENIED"
        denied = await api.post(
            "/executions",
            headers=payments,
            json={
                "tool_id": "support.create_ticket",
                "arguments": {
                    "customer_id": "C-102",
                    "order_id": "O-102",
                    "subject": "Forbidden",
                },
            },
        )
        assert denied.json()["status"] == "DENIED"
        evidence = await api.get(f"/executions/{denied.json()['id']}", headers=payments)
        assert evidence.json()["tool_id"] is None
        assert (
            await api.get(f"/executions/{trace_id}", headers=payments)
        ).status_code == 404
        assert (await api.get(f"/executions/{trace_id}", headers=admin)).json()[
            "status"
        ] == "SUCCEEDED"
        assert (await api.post("/auth/logout", headers=support)).status_code == 204
        assert (await api.get("/auth/me", headers=support)).status_code == 401

    asyncio.run(scenario())
