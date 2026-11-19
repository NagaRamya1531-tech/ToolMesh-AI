"""Measure an isolated sandbox read and exercise its outage and recovery paths."""

import argparse
import asyncio
import json
import os
import platform
import re
import subprocess
import time
from collections import Counter
from uuid import uuid4

import httpx

TOOL = "payments.get_transaction"
PAYLOAD = {
    "tool_id": TOOL,
    "arguments": {"customer_id": "C-102", "transaction_id": "TX-102-A"},
}


def compose(project: str, *commands: str, input_sql: str | None = None) -> str:
    return subprocess.check_output(
        ["docker", "compose", "-p", project, *commands],
        input=input_sql,
        text=True,
    )


async def measure(origin: str, project: str | None) -> dict:
    async with httpx.AsyncClient(base_url=origin, timeout=15, trust_env=False) as api:

        async def login(user: str, variable: str) -> dict:
            response = await api.post(
                "/api/v1/auth/login",
                json={
                    "username": user,
                    "password": os.environ[variable],
                },
            )
            response.raise_for_status()
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        admin = await login("platform-admin", "TOOLMESH_ADMIN_PASSWORD")
        caller = await login("payments-agent", "TOOLMESH_PAYMENTS_PASSWORD")
        publication = await api.post(
            f"/api/v1/tools/{TOOL}/publish",
            headers=admin,
            json={
                "version": 1,
                "reason": "Isolated M6 operations rehearsal",
            },
        )
        publication.raise_for_status()

        async def invoke() -> dict:
            start = time.perf_counter()
            response = await api.post(
                "/api/v1/executions", headers=caller, json=PAYLOAD
            )
            elapsed_ms = round(1000 * (time.perf_counter() - start), 2)
            response.raise_for_status()
            result = response.json()
            return {
                **{k: result[k] for k in ("status", "error_code", "id")},
                "request_id": response.headers["X-Request-ID"],
                "elapsed_ms": elapsed_ms,
            }

        assert (await invoke())["status"] == "SUCCEEDED"
        samples = []
        for _ in range(5):
            samples.extend(await asyncio.gather(*(invoke() for _ in range(4))))
        assert Counter(row["status"] for row in samples) == {"SUCCEEDED": 20}, samples
        assert len({row["request_id"] for row in samples}) == 20
        assert len({row["id"] for row in samples}) == 20
        selected = samples[-1]
        path = f"/api/v1/executions/{selected['id']}"
        trace = (await api.get(path, headers=caller)).json()
        assert trace["request_id"] == selected["request_id"]
        assert len(trace["otel_trace_id"]) == 32
        timeline = (await api.get(f"{path}/events", headers=caller)).json()
        assert [event["status"] for event in timeline] == [
            "RECEIVED",
            "RUNNING",
            "SUCCEEDED",
        ]
        report = {
            "host": platform.platform(),
            "python": platform.python_version(),
            "operation": TOOL,
            "parallelism": 4,
            "warmups": 1,
            "samples": samples,
        }

        if project:
            baseline = (await api.get("/metrics")).text
            markers = {
                "retry": 'toolmesh_read_dispatch_events_total{event="retry",service="payments"}',
                "opened": 'toolmesh_read_dispatch_events_total{event="opened",service="payments"}',
                "rejected": 'toolmesh_read_dispatch_events_total{event="rejected",service="payments"}',
            }

            def counter(body, key):
                return next(
                    (
                        float(line.split()[-1])
                        for line in body.splitlines()
                        if line.startswith(markers[key] + " ")
                    ),
                    0,
                )

            compose(project, "stop", "payments")
            try:
                outage = [await invoke() for _ in range(4)]
                assert [row["error_code"] for row in outage] == [
                    "UPSTREAM_ERROR",
                    "UPSTREAM_ERROR",
                    "UPSTREAM_ERROR",
                    "UPSTREAM_CIRCUIT_OPEN",
                ], outage
                metrics = (await api.get("/metrics")).text
                delta = {
                    key: counter(metrics, key) - counter(baseline, key)
                    for key in markers
                }
                assert delta == {"retry": 6, "opened": 1, "rejected": 1}, delta
            finally:
                compose(project, "up", "-d", "--wait", "payments")
            await asyncio.sleep(15)
            recovered = await invoke()
            assert recovered["status"] == "SUCCEEDED", recovered

            fixtures = {risk: str(uuid4()) for risk in ("read", "write", "received")}
            compose(
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
                input_sql="INSERT INTO executions (id,principal,status,dispatch_risk,started_at) VALUES "
                + ",".join(
                    f"('{key}','payments-agent','{('RECEIVED' if risk == 'received' else 'RUNNING')}',"
                    f"'{risk}',now()-interval '6 minutes')"
                    for risk, key in fixtures.items()
                )
                + ";\n",
            )
            expected = {"read": "FAILED", "write": "UNKNOWN", "received": "FAILED"}
            for _ in range(25):
                states = {
                    risk: (
                        await api.get(f"/api/v1/executions/{key}", headers=caller)
                    ).json()["status"]
                    for risk, key in fixtures.items()
                }
                if states == expected:
                    break
                await asyncio.sleep(0.5)
            assert states == expected, states
            compose(project, "stop", "redis")
            try:
                unready = await api.get("/readyz")
                assert unready.status_code == 503
                refused = await invoke()
                assert refused["error_code"] == "QUOTA_UNAVAILABLE", refused
            finally:
                compose(project, "up", "-d", "--wait", "redis")

            report["fault_rehearsal"] = {
                "outage": outage,
                "counters": delta,
                "recovered": recovered,
                "stale_classification": states,
                "redis_unready": unready.json()["checks"],
                "redis_invocation": refused,
            }
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, choices=["http://127.0.0.1:18009"])
    parser.add_argument(
        "--fault-project", help="Disposable Compose project to interrupt"
    )
    args = parser.parse_args()
    origin = args.url
    project = args.fault_project
    if project and not re.fullmatch(r"toolmesh-m6-fault(?:-[a-z0-9]+)?", project):
        parser.error("Use a dedicated toolmesh-m6-fault Compose project.")
    if (
        project
        and f"http://{compose(project, 'port', 'api', '8000').strip()}" != origin
    ):
        parser.error("Compose project does not own the requested URL.")
    print(json.dumps(asyncio.run(measure(origin, project)), indent=2))


if __name__ == "__main__":
    main()
