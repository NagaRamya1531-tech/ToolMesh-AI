"""Run a real evaluation against a configured local ToolMesh sandbox."""

import argparse
import gzip
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--definitions", type=int, default=1000)
    parser.add_argument("--cases", type=int, default=120)
    parser.add_argument("--split", choices=["test", "tuning"], default="test")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    password = os.getenv("TOOLMESH_ADMIN_PASSWORD") or dotenv_values(".env").get(
        "TOOLMESH_ADMIN_PASSWORD"
    )
    if not password:
        parser.error("Set TOOLMESH_ADMIN_PASSWORD or provide the sandbox .env file")
    with httpx.Client(
        base_url=args.url.rstrip("/") + "/api/v1", timeout=20, trust_env=False
    ) as api:
        login = api.post(
            "/auth/login", json={"username": "platform-admin", "password": password}
        )
        login.raise_for_status()
        api.headers["Authorization"] = "Bearer " + login.json()["access_token"]
        try:
            result = api.post(
                "/evaluations/runs",
                json={
                    "catalog_size": args.definitions,
                    "case_count": args.cases,
                    "split": args.split,
                },
            )
            if result.is_error:
                raise SystemExit(
                    result.json()
                    .get("error", {})
                    .get("code", "EVALUATION_SUBMIT_FAILED")
                )
            run_id = result.json()["id"]
            print(f"Evaluation {run_id}", flush=True)
            previous = None
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                response = api.get(f"/evaluations/runs/{run_id}")
                response.raise_for_status()
                run = response.json()["run"]
                stage = run["stage"]
                if stage != previous:
                    print(stage, flush=True)
                    previous = stage
                if run["status"] == "FAILED":
                    raise SystemExit(run["error_code"])
                if run["status"] == "SUCCEEDED":
                    break
                time.sleep(1)
            else:
                raise SystemExit("Evaluation did not finish before the client deadline")
            response = api.get(
                f"/evaluations/runs/{run_id}/export", params={"format": "json"}
            )
            response.raise_for_status()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            if args.output.suffix == ".gz":
                args.output.write_bytes(gzip.compress(response.content, mtime=0))
            else:
                args.output.write_bytes(response.content)
            print(json.dumps(run["summary"]["strategies"], indent=2))
            print(f"Export saved to {args.output}")
        finally:
            api.post("/auth/logout")


if __name__ == "__main__":
    main()
