"""Exercise the sandbox across separate Support, Finance, and Approver sessions."""

import asyncio
import os
from uuid import UUID, uuid4

import httpx
import pytest

ORIGIN = os.getenv("TOOLMESH_TEST_API")
pytestmark = pytest.mark.skipif(not ORIGIN, reason="requires Compose and sandbox data")
DELAYED = os.getenv("TOOLMESH_TEST_REFUND_DELAY") == "1"


def test_duplicate_refund_workflow():
    asyncio.run(workflow())


async def workflow():
    async with httpx.AsyncClient(base_url=f"{ORIGIN}/api/v1", timeout=12) as api:

        async def login(username, env, default):
            response = await api.post(
                "/auth/login",
                json={
                    "username": username,
                    "password": os.getenv(env, default),
                },
            )
            assert response.status_code == 200, response.text
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        admin = await login(
            "platform-admin", "TOOLMESH_ADMIN_PASSWORD", "local_admin_password"
        )
        support = await login(
            "support-agent", "TOOLMESH_SUPPORT_PASSWORD", "local_support_password"
        )
        finance = await login(
            "finance-agent", "TOOLMESH_FINANCE_PASSWORD", "local_finance_password"
        )
        approver = await login(
            "refund-approver", "TOOLMESH_APPROVER_PASSWORD", "local_approver_password"
        )
        tool_ids = (
            "payments.check_duplicate_payment",
            "payments.get_transaction",
            "support.create_ticket",
            "support.get_ticket",
            "support.resolve_ticket",
            "payments.issue_refund",
            "payments.get_refund_status",
            "communication.send_notification",
        )
        for tool in tool_ids:
            res = await api.post(
                f"/tools/{tool}/publish",
                headers=admin,
                json={"version": 1, "reason": "Review workflow contracts"},
            )
            assert res.status_code == 200, res.text

        async def invoke(caller, tool, args, key=None):
            req = {"tool_id": tool, "arguments": {"customer_id": "C-102", **args}}
            if key:
                req["idempotency_key"] = key
            res = await api.post("/executions", headers=caller, json=req)
            assert res.status_code == 200, res.text
            return res.json()

        async def decision(caller, approval_id, choice="approve"):
            return await api.post(
                f"/approvals/{approval_id}/{choice}",
                headers=caller,
                json={"reason": "Verified duplicate capture evidence"},
            )

        duplicate = await invoke(
            support, "payments.check_duplicate_payment", {"order_id": "O-102"}
        )
        assert duplicate["result"]["groups"] == [["TX-102-A", "TX-102-B"]]
        assert not (
            await invoke(
                support, "payments.check_duplicate_payment", {"order_id": "O-103"}
            )
        )["result"]["duplicate_capture_detected"]
        ticket = await invoke(
            support,
            "support.create_ticket",
            {
                "order_id": "O-102",
                "subject": "Duplicate captures TX-102-A and TX-102-B",
            },
        )
        assert ticket["status"] == "SUCCEEDED", ticket
        ticket_id = ticket["result"]["id"]

        refund = {"transaction_id": "TX-102-B", "currency": "USD", "amount_minor": 4550}
        denied = await invoke(support, "payments.issue_refund", refund, str(uuid4()))
        assert (
            denied["status"] == "DENIED"
            and denied["error_code"] == "TOOL_NOT_AVAILABLE"
        )
        rejected = await invoke(finance, "payments.issue_refund", refund, str(uuid4()))
        assert rejected["status"] == "PENDING_APPROVAL", rejected
        assert (await decision(finance, rejected["id"])).status_code == 403
        assert (await decision(admin, rejected["id"])).status_code == 403
        assert (await decision(approver, rejected["id"], "reject")).json()[
            "status"
        ] == "REJECTED"
        assert (await decision(approver, rejected["id"])).status_code == 409
        assert (
            await invoke(
                support, "payments.get_refund_status", {"refund_id": rejected["id"]}
            )
        )["status"] == "FAILED"

        key = "m2-primary-workflow-refund-v1"
        pending = await invoke(finance, "payments.issue_refund", refund, key)
        approval_id = pending["approval_id"]
        assert approval_id and pending["status"] in {"PENDING_APPROVAL", "SUCCEEDED"}, (
            pending
        )
        repeated = await invoke(finance, "payments.issue_refund", refund, key)
        assert repeated["id"] == pending["id"]
        assert "intent_hash" not in repeated
        conflict = await invoke(
            finance, "payments.issue_refund", {**refund, "amount_minor": 4549}, key
        )
        assert conflict["error_code"] == "IDEMPOTENCY_CONFLICT"

        if pending["status"] == "PENDING_APPROVAL":
            premature = await invoke(
                support, "communication.send_notification", {"refund_id": approval_id}
            )
            assert premature["status"] == "DENIED"
            decisions = await asyncio.gather(
                decision(approver, approval_id), decision(approver, approval_id)
            )
            assert sorted(result.status_code for result in decisions) == [200, 409]
            outcome = next(
                result.json() for result in decisions if result.status_code == 200
            )
            assert outcome["status"] == ("UNKNOWN" if DELAYED else "SUCCEEDED"), outcome
            if DELAYED:
                for _ in range(24):
                    trace = await api.get(
                        f"/executions/{approval_id}", headers=approver
                    )
                    if trace.json()["status"] == "SUCCEEDED":
                        break
                    await asyncio.sleep(0.5)
                else:
                    pytest.fail(
                        "Refund ledger reconciliation did not confirm the outcome"
                    )

        confirmed = await invoke(
            support, "payments.get_refund_status", {"refund_id": approval_id}
        )
        assert confirmed["result"]["amount_minor"] == 4550
        early_resolution = await invoke(
            support,
            "support.resolve_ticket",
            {"ticket_id": ticket_id, "refund_id": approval_id},
        )
        if pending["status"] == "PENDING_APPROVAL":
            assert early_resolution["status"] == "DENIED"
        notification = await invoke(
            support, "communication.send_notification", {"refund_id": approval_id}
        )
        assert notification["status"] == "SUCCEEDED", notification
        assert notification["result"]["status"] == "SIMULATED"
        assert (
            await invoke(
                support, "communication.send_notification", {"refund_id": approval_id}
            )
        )["result"]["id"] == notification["result"]["id"]
        resolution = await invoke(
            support,
            "support.resolve_ticket",
            {"ticket_id": ticket_id, "refund_id": approval_id},
        )
        assert resolution["result"]["status"] == "resolved", resolution
        assert (await invoke(support, "support.get_ticket", {"ticket_id": ticket_id}))[
            "result"
        ]["status"] == "resolved"
        over_refund = await invoke(
            finance,
            "payments.issue_refund",
            {**refund, "amount_minor": 1},
            str(uuid4()),
        )
        outcome = await decision(approver, over_refund["id"])
        assert outcome.json()["error_code"] == "REFUND_CONFLICT", outcome.text

        audit = await api.get("/audit-events", headers=approver)
        assert {event["action"] for event in audit.json()} >= {
            "REFUND_APPROVED",
            "REFUND_SUCCEEDED",
            "REFUND_REJECTED",
        }
        assert (await api.get("/audit-events", headers=support)).status_code == 403
        overview = await api.get("/overview?hours=24", headers=admin)
        assert overview.status_code == 200, overview.text
        assert any(row["status"] == "SUCCEEDED" for row in overview.json()["outcomes"])


@pytest.mark.skipif(
    os.getenv("TOOLMESH_TEST_MUTATE_POLICY") != "1",
    reason="requires a disposable Compose sandbox for direct fixture setup",
)
def test_expiry_and_grant_revocation_are_enforced_at_approval():
    import subprocess

    def sql(statement):
        project = os.getenv("TOOLMESH_TEST_COMPOSE_PROJECT", "toolmesh")
        return subprocess.check_output(
            [
                "docker",
                "compose",
                "-p",
                project,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-XAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "toolmesh_migrator",
                "-d",
                "toolmesh",
                "-c",
                statement,
            ],
            text=True,
        ).strip()

    async def scenario():
        async with httpx.AsyncClient(base_url=f"{ORIGIN}/api/v1", timeout=10) as api:

            async def session(name, variable, fallback):
                response = await api.post(
                    "/auth/login",
                    json={"username": name, "password": os.getenv(variable, fallback)},
                )
                response.raise_for_status()
                return {"Authorization": f"Bearer {response.json()['access_token']}"}

            finance = await session(
                "finance-agent", "TOOLMESH_FINANCE_PASSWORD", "local_finance_password"
            )
            reviewer = await session(
                "refund-approver",
                "TOOLMESH_APPROVER_PASSWORD",
                "local_approver_password",
            )

            async def pending():
                result = await api.post(
                    "/executions",
                    headers=finance,
                    json={
                        "tool_id": "payments.issue_refund",
                        "idempotency_key": str(uuid4()),
                        "arguments": {
                            "customer_id": "C-102",
                            "transaction_id": "TX-103-A",
                            "amount_minor": 1,
                            "currency": "USD",
                        },
                    },
                )
                assert result.json()["status"] == "PENDING_APPROVAL", result.text
                return str(UUID(result.json()["approval_id"]))

            expired = await pending()
            sql(
                f"UPDATE approvals SET expires_at=now()-interval '1 second' WHERE id='{expired}'"
            )
            response = await api.post(
                f"/approvals/{expired}/approve",
                headers=reviewer,
                json={"reason": "Expiry test"},
            )
            assert response.status_code == 409, response.text
            trace = await api.get(f"/executions/{expired}", headers=reviewer)
            assert trace.json()["status"] == "EXPIRED"

            revoked = await pending()
            sql(
                "UPDATE refund_grants SET enabled=false WHERE principal='finance-agent'"
            )
            try:
                response = await api.post(
                    f"/approvals/{revoked}/approve",
                    headers=reviewer,
                    json={"reason": "Revocation test"},
                )
                assert response.status_code == 409, response.text
                assert (
                    response.json()["error"]["code"] == "REFUND_AUTHORIZATION_CHANGED"
                )
                schema = await api.get(
                    "/discovery/tools/payments.issue_refund/schema", headers=finance
                )
                assert schema.status_code == 404
            finally:
                sql(
                    "UPDATE refund_grants SET enabled=true WHERE principal='finance-agent'"
                )
            await api.post(
                f"/approvals/{revoked}/reject",
                headers=reviewer,
                json={"reason": "Close test approval"},
            )
            assert (
                sql(
                    f"SELECT count(*) FROM payments.refunds WHERE id IN ('{expired}','{revoked}')"
                )
                == "0"
            )

    asyncio.run(scenario())
