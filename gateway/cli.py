"""Operate a ToolMesh gateway using its public REST API."""

import argparse
import getpass
import json
import os
import sys
from urllib.parse import quote

import httpx


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default=os.getenv("TOOLMESH_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument("--user", help="Prompt for this identity's password")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    commands.add_parser("ready")
    discover = commands.add_parser("discover")
    discover.add_argument("query")
    discover.add_argument(
        "--strategy", choices=["keyword", "semantic", "hybrid"], default="keyword"
    )
    schema = commands.add_parser("schema")
    schema.add_argument("tool")
    invoke = commands.add_parser("invoke")
    invoke.add_argument("tool")
    invoke.add_argument("--idempotency-key")
    invoke.add_argument(
        "--input",
        type=argparse.FileType("r"),
        default=sys.stdin,
        help="JSON arguments file (defaults to stdin)",
    )
    trace = commands.add_parser("trace")
    trace.add_argument("id")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    try:
        url = httpx.URL(args.url)
    except httpx.InvalidURL:
        print("Provide a valid gateway URL.", file=sys.stderr)
        return 2
    if url.scheme != "https" and (
        url.scheme != "http" or url.host not in {"localhost", "127.0.0.1", "::1"}
    ):
        print("Use HTTPS for remote gateways.", file=sys.stderr)
        return 2
    if url.username or url.password or url.query or url.fragment or url.path != "/":
        print(
            "Provide the gateway origin without credentials, path, or query.",
            file=sys.stderr,
        )
        return 2
    try:
        with httpx.Client(base_url=url, timeout=15, trust_env=False) as client:
            action = args.command
            if action in {"health", "ready"}:
                response = client.get("/healthz" if action == "health" else "/readyz")
                print(json.dumps(response.json(), indent=2))
                return 0 if response.is_success else 1
            token = os.getenv("TOOLMESH_TOKEN")
            if args.user:
                login = client.post(
                    "/api/v1/auth/login",
                    json={
                        "username": args.user,
                        "password": getpass.getpass("ToolMesh password: "),
                    },
                )
                login.raise_for_status()
                token = login.json()["access_token"]
            if not token:
                print("Set TOOLMESH_TOKEN or use --user.", file=sys.stderr)
                return 2
            client.headers["Authorization"] = f"Bearer {token}"
            root = "/api/v1"
            if action == "discover":
                response = client.post(
                    f"{root}/discovery/search",
                    json={"query": args.query, "strategy": args.strategy},
                )
            elif action == "schema":
                response = client.get(
                    f"{root}/discovery/tools/{quote(args.tool, safe='')}/schema"
                )
            elif action == "invoke":
                payload = json.load(args.input)
                if not isinstance(payload, dict):
                    print(
                        "Invocation arguments must be a JSON object.", file=sys.stderr
                    )
                    return 2
                response = client.post(
                    f"{root}/executions",
                    json={
                        "tool_id": args.tool,
                        "arguments": payload,
                        "idempotency_key": args.idempotency_key,
                    },
                )
            else:
                path = f"{root}/executions/{quote(args.id, safe='')}"
                detail = client.get(path)
                detail.raise_for_status()
                response = client.get(f"{path}/events")
                response.raise_for_status()
                print(
                    json.dumps(
                        {"execution": detail.json(), "events": response.json()},
                        indent=2,
                    )
                )
                return 0
            response.raise_for_status()
            result = response.json()
            print(json.dumps(result, indent=2))
            return int(
                action == "invoke"
                and result["status"] not in {"SUCCEEDED", "PENDING_APPROVAL"}
            )
    except httpx.HTTPStatusError as exc:
        print(
            f"Gateway returned HTTP {exc.response.status_code} (request {exc.response.headers.get('X-Request-ID', 'unavailable')}).",
            file=sys.stderr,
        )
        return 1
    except (httpx.RequestError, ValueError, OSError):
        print("Gateway communication or JSON input failed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
