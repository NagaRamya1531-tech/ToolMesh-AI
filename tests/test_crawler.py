"""Exercise worker, release lifecycle, and gRPC against the Compose sandbox."""

import asyncio
import os
import subprocess

import httpx
import pytest

ORIGIN = os.getenv("TOOLMESH_TEST_API")
pytestmark = pytest.mark.skipif(not ORIGIN, reason="requires Compose sandbox")


def test_discover_publish_and_route_rest_mcp_and_grpc():
    asyncio.run(scenario())


async def scenario():
    async with httpx.AsyncClient(base_url=f"{ORIGIN}/api/v1", timeout=12) as api:

        async def login(username, env, default):
            response = await api.post(
                "/auth/login",
                json={"username": username, "password": os.getenv(env, default)},
            )
            response.raise_for_status()
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        admin = await login(
            "platform-admin", "TOOLMESH_ADMIN_PASSWORD", "local_admin_password"
        )
        it = await login("it-agent", "TOOLMESH_IT_PASSWORD", "local_it_agent_password")
        unauthorized = await api.post(
            "/services", headers=it, json={"target": "inventory", "name": "Inventory"}
        )
        assert unauthorized.status_code == 403
        denied = await api.post(
            "/services",
            headers=admin,
            json={"target": "http://127.0.0.1:8000", "name": "Invalid"},
        )
        assert denied.status_code == 422
        for target, name in [
            ("inventory", "Sandbox Inventory"),
            ("it", "IT Operations"),
        ]:
            made = await api.post(
                "/services", headers=admin, json={"target": target, "name": name}
            )
            assert made.status_code in (201, 409), made.text

        async def crawl(target):
            job = await api.post(f"/services/{target}/crawl", headers=admin, json={})
            assert job.status_code == 202, job.text
            for _ in range(35):
                current = await api.get(
                    f"/crawl-jobs/{job.json()['id']}", headers=admin
                )
                value = current.json()
                if value["status"] not in {"QUEUED", "RUNNING"}:
                    assert value["status"] == "SUCCEEDED", value
                    return value["changes"]
                await asyncio.sleep(0.35)
            pytest.fail(f"{target} crawl did not finish")

        async def invoke(tool, args):
            response = await api.post(
                "/executions",
                headers=it,
                json={"tool_id": tool, "arguments": {"customer_id": "C-102", **args}},
            )
            assert response.status_code == 200, response.text
            return response.json()

        async def release(tool, version, action="publish"):
            return await api.post(
                f"/tools/{tool}/{action}",
                headers=admin,
                json={"version": version, "reason": "Verify release lifecycle"},
            )

        for target in ("payments", "support", "communication", "it", "inventory"):
            changes = await crawl(target)
            assert any(
                change.get("tool_id", "").startswith(target + ".") for change in changes
            ), changes
        catalog = (await api.get("/tools", headers=admin)).json()
        payments = "payments.check_duplicate_payment"
        newer = max(item["version"] for item in catalog if item["id"] == payments)
        assert (await release(payments, 1)).status_code == 200
        assert (await release(payments, newer)).status_code == 200
        assert (await release(payments, 1, "rollback")).status_code == 200

        inventory = "inventory.get_inventory"
        inv_version = max(
            item["version"] for item in catalog if item["id"] == inventory
        )
        assert (await invoke(inventory, {"item_id": "SKU-102"}))["status"] == "DENIED"
        assert (await release(inventory, inv_version, "review")).status_code == 200
        assert (await release(inventory, inv_version)).status_code == 200
        result = await invoke(inventory, {"item_id": "SKU-102"})
        assert result["result"]["quantity"] == 12, result
        for tool in (
            "it.check_service_health",
            "it.create_incident",
            "it.list_incidents",
        ):
            published = await release(tool, 1)
            assert published.status_code == 200, published.text
        health = await invoke("it.check_service_health", {"service_id": "sandbox-api"})
        assert health["result"]["status"] == "degraded", health
        assert (
            await invoke("it.check_service_health", {"service_id": "restricted-api"})
        )["status"] == "FAILED"
        incident = await invoke(
            "it.create_incident",
            {"service_id": "sandbox-api", "title": "Investigate simulated latency"},
        )
        assert incident["status"] == "SUCCEEDED", incident
        results = await invoke("it.list_incidents", {})
        assert incident["result"]["id"] in {
            item["id"] for item in results["result"]["incidents"]
        }

        if os.getenv("TOOLMESH_TEST_MUTATE_POLICY") == "1":

            def mode(required):
                project = os.getenv("TOOLMESH_TEST_COMPOSE_PROJECT", "toolmesh")
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-p",
                        project,
                        "exec",
                        "-T",
                        "postgres",
                        "psql",
                        "-X",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-U",
                        "toolmesh_migrator",
                        "-d",
                        "toolmesh",
                        "-c",
                        "UPDATE inventory.schema_state SET require_location="
                        + ("true" if required else "false"),
                    ],
                    check=True,
                    capture_output=True,
                )

            mode(True)
            try:
                drift = next(
                    change
                    for change in await crawl("inventory")
                    if change.get("tool_id") == inventory
                )
                assert drift["status"] == "quarantined", drift
                assert drift["diff"]["input_schema"]["required_added"] == ["location"]
                assert (await invoke(inventory, {"item_id": "SKU-102"}))[
                    "status"
                ] == "DENIED"
                assert (await release(inventory, inv_version)).status_code == 409
                assert (await release(inventory, drift["version"])).status_code == 200
                assert (await invoke(inventory, {"item_id": "SKU-102"}))[
                    "error_code"
                ] == "INVALID_ARGUMENT"
                result = await invoke(
                    inventory, {"item_id": "SKU-102", "location": "EAST"}
                )
                assert result["result"]["quantity"] == 12, result
                history = (
                    await api.get(f"/tools/{inventory}/versions", headers=admin)
                ).json()
                assert "quarantine" in {event["action"] for event in history["history"]}
            finally:
                mode(False)
                cleaned = next(
                    change
                    for change in await crawl("inventory")
                    if change.get("tool_id") == inventory
                )
                restored = await release(inventory, cleaned["version"])
                assert restored.status_code == 200, restored.text
        tool = "it.list_incidents"
        assert (await release(tool, 1, "disable")).status_code == 200
        assert (await invoke(tool, {}))["status"] == "DENIED"
        assert (await release(tool, 1)).status_code == 200
