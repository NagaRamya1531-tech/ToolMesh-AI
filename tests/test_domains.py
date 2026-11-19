"""Exercise every required business operation and governance against real services."""

import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

ORIGIN = os.getenv("TOOLMESH_TEST_API")
pytestmark = pytest.mark.skipif(
    not ORIGIN, reason="requires a writable disposable Compose sandbox"
)

REQUIRED = {
    "payments": "get_transaction list_transactions check_duplicate_payment issue_refund get_refund_status get_payment_method",
    "support": "get_customer create_ticket update_ticket search_tickets resolve_ticket escalate_ticket",
    "it": "check_service_health get_system_logs list_incidents restart_service create_incident get_deployment_status",
    "hr": "get_employee get_department get_leave_balance request_leave get_manager get_company_policy",
    "analytics": "get_daily_revenue get_sales_metrics get_customer_metrics generate_report get_refund_metrics compare_periods",
    "communication": "send_email send_notification search_messages get_message create_channel get_channel_members",
}
PROFILES = {
    "platform-admin": ("TOOLMESH_ADMIN_PASSWORD", "local_admin_password"),
    "support-agent": ("TOOLMESH_SUPPORT_PASSWORD", "local_support_password"),
    "finance-agent": ("TOOLMESH_FINANCE_PASSWORD", "local_finance_password"),
    "it-agent": ("TOOLMESH_IT_PASSWORD", "local_it_agent_password"),
    "hr-agent": ("TOOLMESH_HR_PASSWORD", "local_hr_agent_password"),
    "refund-approver": ("TOOLMESH_APPROVER_PASSWORD", "local_approver_password"),
}


def login(client, name, credential=None):
    password = credential or os.getenv(PROFILES[name][0], PROFILES[name][1])
    response = client.post("/auth/login", json={"username": name, "password": password})
    assert response.is_success, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def wait(client, headers, path, states):
    for _ in range(70):
        result = client.get(path, headers=headers)
        assert result.is_success, result.text
        data = result.json()
        if data["status"] in states:
            return data
        time.sleep(0.2)
    pytest.fail(f"Waiting for {states} timed out")


def test_complete_catalog_and_scripted_profiles():
    with httpx.Client(base_url=f"{ORIGIN}/api/v1", timeout=15) as client:
        admin = login(client, "platform-admin")
        support = login(client, "support-agent")
        finance = login(client, "finance-agent")
        it = login(client, "it-agent")
        hr = login(client, "hr-agent")
        approver = login(client, "refund-approver")
        required = {
            f"{service}.{op}" for service, ops in REQUIRED.items() for op in ops.split()
        }
        assert len(required) == 36
        catalog = client.get("/tools", headers=admin).json()
        for identifier in sorted(required | {"communication.notify_leave"}):
            versions = [row for row in catalog if row["id"] == identifier]
            assert versions, identifier
            selected = next(
                (row["active_version"] for row in versions if row["active_version"]),
                None,
            ) or max(row["version"] for row in versions)
            publication = client.post(
                f"/tools/{identifier}/publish",
                headers=admin,
                json={"version": selected, "reason": "Verify domain contracts"},
            )
            assert publication.status_code == 200, (identifier, publication.text)
        seen = set()

        def invoke(actor, tool, args=None, state="SUCCEEDED", **extra):
            outcome = client.post(
                "/executions",
                headers=actor,
                json={
                    "tool_id": tool,
                    "arguments": {"customer_id": "C-102", **(args or {})},
                    **extra,
                },
            )
            assert outcome.is_success, (tool, outcome.text)
            data = outcome.json()
            assert data["status"] == state, (tool, data)
            if state in {"SUCCEEDED", "PENDING_APPROVAL"}:
                seen.add(tool)
            return data if state != "SUCCEEDED" else data["result"]

        def approve(id):
            result = client.post(
                f"/approvals/{id}/approve",
                headers=approver,
                json={"reason": "Verified sandbox change"},
            )
            assert result.is_success, result.text
            assert result.json()["status"] == "SUCCEEDED", result.text
            return result.json()["result"]

        assert invoke(
            support, "payments.get_payment_method", {"transaction_id": "TX-102-A"}
        ) == {"brand": "Sandbox Visa", "last_four": "4242"}
        invoke(support, "payments.get_transaction", {"transaction_id": "TX-102-A"})
        invoke(support, "payments.list_transactions")
        assert invoke(
            support, "payments.check_duplicate_payment", {"order_id": "O-102"}
        )["duplicate_capture_detected"]
        refund = invoke(
            finance,
            "payments.issue_refund",
            {"transaction_id": "TX-103-A", "amount_minor": 1, "currency": "USD"},
            "PENDING_APPROVAL",
            idempotency_key=str(uuid4()),
        )
        assert approve(refund["id"])["id"] == refund["id"]
        assert (
            invoke(finance, "payments.get_refund_status", {"refund_id": refund["id"]})[
                "amount_minor"
            ]
            == 1
        )
        invoke(support, "support.get_customer")
        ticket = invoke(
            support,
            "support.create_ticket",
            {"order_id": "O-103", "subject": "Sandbox issue"},
        )
        assert ticket["id"] in {
            item["id"] for item in invoke(support, "support.search_tickets")["tickets"]
        }
        change = {"ticket_id": ticket["id"], "note": "Investigate sandbox capture"}
        assert (
            invoke(
                support, "support.update_ticket", {**change, "status": "investigating"}
            )["status"]
            == "investigating"
        )
        invoke(
            support,
            "support.update_ticket",
            {**change, "status": "investigating"},
            "FAILED",
        )
        assert (
            invoke(support, "support.escalate_ticket", change)["status"] == "escalated"
        )
        assert (
            invoke(
                support, "communication.send_notification", {"refund_id": refund["id"]}
            )["status"]
            == "SIMULATED"
        )
        assert (
            invoke(
                support,
                "support.resolve_ticket",
                {"ticket_id": ticket["id"], "refund_id": refund["id"]},
            )["status"]
            == "resolved"
        )
        fake_value = "sample-sensitive-value"
        mail = invoke(
            support,
            "communication.send_email",
            {
                "recipient_id": "C-102",
                "subject": "Sandbox update",
                "body": "password=" + fake_value,
            },
        )
        assert mail["body"] == "[REDACTED]"
        assert invoke(
            support,
            "communication.get_message",
            {"message_id": mail["id"]},
            fields=["body"],
        ) == {"body": "[REDACTED]"}
        assert mail["id"] in {
            item["id"]
            for item in invoke(
                support,
                "communication.search_messages",
                {"query": "Sandbox update", "limit": 25},
            )["messages"]
        }
        channel = {"channel_name": "test-" + uuid4().hex}
        assert invoke(support, "communication.create_channel", channel)["members"] == [
            "C-102"
        ]
        assert (
            invoke(support, "communication.get_channel_members", channel)[
                "channel_name"
            ]
            == channel["channel_name"]
        )

        service = {"service_id": "sandbox-api"}
        invoke(it, "it.check_service_health", service)
        assert invoke(
            it, "it.get_system_logs", {**service, "level": "ERROR", "limit": 10}
        )["logs"]
        assert (
            invoke(it, "it.get_deployment_status", service)["release"]
            == "sandbox-2026.11"
        )
        incident = invoke(
            it, "it.create_incident", {**service, "title": "Synthetic incident"}
        )
        assert incident["id"] in {
            item["id"] for item in invoke(it, "it.list_incidents")["incidents"]
        }

        def scenario(actor, args, expected):
            request_id = str(uuid4())
            start = client.post(
                "/playground/runs",
                headers=actor,
                json={"request_id": request_id, "arguments": args},
            )
            assert start.status_code == 202, start.text
            return wait(
                client,
                actor,
                f"/playground/runs/{request_id}",
                expected | {"INTERRUPTED", "STOPPED"},
            )

        it_run = scenario(it, service, {"WAITING_FOR_APPROVAL"})
        assert it_run["status"] == "WAITING_FOR_APPROVAL", it_run
        assert it_run["steps"][-1]["tool"] == "it.restart_service"
        seen.add("it.restart_service")
        assert (
            client.post(
                f"/playground/runs/{it_run['id']}/resume", headers=it, json={}
            ).status_code
            == 409
        )
        assert approve(it_run["approval_id"])["service_id"] == "sandbox-api"
        assert (
            client.post(
                f"/playground/runs/{it_run['id']}/resume", headers=it, json={}
            ).status_code
            == 202
        )
        recovered = wait(
            client,
            it,
            f"/playground/runs/{it_run['id']}",
            {"SUCCEEDED", "STOPPED", "INTERRUPTED"},
        )
        assert recovered["status"] == "SUCCEEDED", recovered
        assert recovered["steps"][-1]["summary"]["status"] == "healthy"

        employee = {"employee_id": "E-102"}
        assert invoke(hr, "hr.get_employee", employee)["employee_id"] == "E-102"
        invoke(hr, "hr.get_employee", {"employee_id": "E-103"}, "DENIED")
        assert invoke(hr, "hr.get_manager", employee)["employee_id"] == "E-103"
        invoke(hr, "hr.get_department", {"department_id": "D-102"})
        invoke(hr, "hr.get_company_policy", {"policy_id": "LEAVE"})
        before = invoke(hr, "hr.get_leave_balance", employee)["available_days"]
        tomorrow = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
        leave = {**employee, "start_date": tomorrow, "end_date": tomorrow}
        hr_run = scenario(hr, leave, {"SUCCEEDED"})
        assert hr_run["status"] == "SUCCEEDED", hr_run
        assert [step["tool"] for step in hr_run["steps"]] == [
            "hr.get_leave_balance",
            "hr.request_leave",
            "communication.notify_leave",
        ]
        seen.add("hr.request_leave")
        assert (
            invoke(hr, "hr.get_leave_balance", employee)["available_days"] == before - 1
        )
        invoke(hr, "hr.request_leave", leave, "FAILED")
        invoke(
            hr,
            "hr.request_leave",
            {
                **leave,
                "start_date": (
                    datetime.now(UTC).date() + timedelta(days=7)
                ).isoformat(),
            },
            "FAILED",
        )

        today = datetime.now(UTC).date()
        insufficient = invoke(
            hr,
            "hr.request_leave",
            {
                **employee,
                "start_date": (today + timedelta(days=35)).isoformat(),
                "end_date": (today + timedelta(days=54)).isoformat(),
            },
            "FAILED",
        )
        assert insufficient["error_code"] == "LEAVE_CONFLICT"

        dates = {"start_date": "2026-11-01", "end_date": "2026-11-30"}
        previous = {
            "previous_start_date": "2026-10-01",
            "previous_end_date": "2026-10-31",
        }
        assert (
            invoke(finance, "analytics.get_daily_revenue", dates)["days"][0][
                "total_minor"
            ]
            == 9100
        )
        assert (
            invoke(finance, "analytics.get_sales_metrics", dates)["total_minor"] == 9100
        )
        assert (
            invoke(finance, "analytics.get_customer_metrics", dates)["repeat_customers"]
            == 1
        )
        invoke(finance, "analytics.get_refund_metrics", dates)
        assert (
            invoke(finance, "analytics.compare_periods", {**dates, **previous})[
                "delta_minor"
            ]
            == 4100
        )
        assert invoke(finance, "analytics.generate_report", dates)["id"]
        assert (
            scenario(finance, dates | previous, {"SUCCEEDED"})["status"] == "SUCCEEDED"
        )
        assert (
            scenario(support, {"order_id": "O-102"}, {"SUCCEEDED"})["status"]
            == "SUCCEEDED"
        )
        assert seen == required, (required - seen, seen - required)


def test_policy_precedence_versions_quota_and_session_revocation():
    with httpx.Client(base_url=f"{ORIGIN}/api/v1", timeout=10) as client:
        admin = login(client, "platform-admin")
        created = client.post(
            "/agents",
            headers=admin,
            json={"id": "test-" + uuid4().hex, "profile": "hr"},
        )
        assert created.status_code == 201, created.text
        info = created.json()
        agent = login(client, info["id"], info["credential"])
        assert all(
            "credential" not in item and "credential_hash" not in item
            for item in client.get("/agents", headers=admin).json()
        )
        assert client.get("/agents", headers=agent).status_code == 403
        tool = "hr.get_employee"
        query = {"principal": info["id"], "tool_id": tool}
        simulation = {
            **query,
            "arguments": {"customer_id": "C-102", "employee_id": "E-102"},
        }
        assert client.post("/policies/simulate", headers=admin, json=simulation).json()[
            "allowed"
        ]
        allow = client.post(
            "/policies",
            headers=admin,
            json={**query, "reason": "Verify precedence", "effect": "allow"},
        )
        assert allow.status_code == 201, allow.text
        deny = client.post(
            "/policies",
            headers=admin,
            json={**query, "reason": "Deny overrides allow", "effect": "deny"},
        )
        assert deny.status_code == 201, deny.text
        assert (
            client.post("/policies/simulate", headers=admin, json=simulation).json()[
                "reason"
            ]
            == "EXPLICIT_DENY"
        )
        assert (
            client.get(f"/discovery/tools/{tool}/schema", headers=agent).status_code
            == 404
        )
        discovery = client.post(
            "/discovery/search", headers=agent, json={"query": "", "service_id": "hr"}
        )
        assert tool not in {item["id"] for item in discovery.json()}
        invoke = {"tool_id": tool, "arguments": simulation["arguments"]}
        assert (
            client.post("/executions", headers=agent, json=invoke).json()["status"]
            == "DENIED"
        )
        id = deny.json()["id"]
        change = {"enabled": False, "version": 1, "reason": "Restore fixture access"}
        assert (
            client.patch(f"/policies/{id}", headers=admin, json=change).status_code
            == 200
        )
        assert (
            client.patch(f"/policies/{id}", headers=admin, json=change).status_code
            == 409
        )
        assert [
            item["version"]
            for item in client.get(f"/policies/{id}/history", headers=admin).json()
        ] == [2, 1]
        saved = client.get("/settings/quotas", headers=admin).json()
        try:
            assert (
                client.patch(
                    "/settings/quotas",
                    headers=admin,
                    json={"per_tool": 1, "global_limit": 10000},
                ).status_code
                == 200
            )
            allowed = client.post("/executions", headers=agent, json=invoke)
            assert allowed.json()["status"] == "SUCCEEDED", allowed.text
            limited = client.post("/executions", headers=agent, json=invoke)
            assert (
                limited.json()["error_code"] == "RATE_LIMITED"
                and limited.headers["Retry-After"] == "60"
            )
        finally:
            client.patch(
                "/settings/quotas", headers=admin, json=saved
            ).raise_for_status()
        revoked = client.patch(
            f"/agents/{info['id']}", headers=admin, json={"enabled": False}
        )
        assert revoked.status_code == 200
        assert client.get("/auth/me", headers=agent).status_code == 401
