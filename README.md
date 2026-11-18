# ToolMesh AI

ToolMesh AI is an enterprise MCP gateway and operations console in development. It includes an approval-gated duplicate-payment workflow, versioned service discovery, and a reproducible evaluation console for keyword, vector, and hybrid retrieval.

## Start the local sandbox

Install Docker with the Compose v2 plugin and start its daemon. From the repository root:

```sh
cp -n .env.example .env
docker compose config --quiet
docker compose up --build -d --wait
```

Open the [local console](http://127.0.0.1:5173) or inspect the [API contract](http://127.0.0.1:8000/docs). Only the API and web ports bind to the host, on loopback. Change `API_PORT` and `WEB_PORT` in `.env` if needed. The passwords in the template are local examples. Choose URL-safe values before first startup. Postgres initializes role passwords only on an empty volume.

The one-shot `migrate` service provisions pgvector, the default sandbox workspace, the M1 catalog and isolated domain schemas. It inserts deterministic synthetic payment fixtures and version 1 tool drafts. The migration exits successfully before the domain services and API start.

When upgrading an earlier checkout, merge the new `TOOLMESH_*` keys from `.env.example` into the existing `.env` before starting Compose. Preserve the original database password values. The domain database passwords provision the domain roles during migration. Changing them after migration requires updating the matching database roles.

```sh
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
docker compose ps -a
docker compose down
```

`docker compose down` keeps the database volume. If startup fails, inspect `docker compose logs postgres migrate api web`. An interrupted first initialization can leave a partially initialized volume. To inspect a clean bootstrap without deleting data, use a different Compose project with unused host ports.

## Verify locally

Use Python 3.12, uv, Node 22 and npm for host checks. Provision the pinned embedding model before the full integration and browser verification, then start the Compose stack.

```sh
uv sync --locked --python 3.12
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy gateway control_plane shared services benchmarks
docker compose run --rm migrate

npm --prefix apps/web ci
npm --prefix apps/web run build
(cd apps/web && npx playwright install chromium)
npm --prefix apps/web run test:e2e -- overview.spec.ts workspace.spec.ts
npm --prefix apps/web run test:e2e -- refund.spec.ts crawler.spec.ts
npm --prefix apps/web run test:e2e -- evaluations.spec.ts
TOOLMESH_TEST_REQUIRE_MODEL=1 TOOLMESH_TEST_API=http://127.0.0.1:8000 uv run --locked pytest -q
```

Omit `TOOLMESH_TEST_API` to run just the standalone Python checks. The integration and browser suites publish sandbox contracts and write test records. Use a disposable Compose project for a clean demonstration, and set `TOOLMESH_EXPECT_DRAFT=1` to require browser publication from an unpublished draft. Override `TOOLMESH_TEST_WEB` for a different console port. On a disposable sandbox, stop Redis with `docker compose stop redis`. A new readiness check should report HTTP 503 with Redis unavailable, while liveness stays HTTP 200. Run `docker compose up -d --wait redis api` to restore service.

For an independent working-tree secret scan:

```sh
docker run --rm -v "$PWD:/repo:ro" ghcr.io/gitleaks/gitleaks:v8.30.0 dir /repo --redact --no-banner --config /repo/.gitleaks.toml
```

## M4 discovery and evaluations

Keyword discovery uses PostgreSQL full-text search and works on a clean sandbox. To enable vector and hybrid search and benchmark runs, explicitly download the pinned CPU model into the read-only model mount, then restart the API.

```sh
uv sync --locked --python 3.12
HF_HUB_DISABLE_XET=1 uv run --locked python -m gateway.embeddings --output .models/discovery
docker compose up --build -d --wait
docker compose restart api
```

In **Playground**, submit a query with **Keyword**, **Vector**, or **Hybrid** selected. Results contain only eligible published tools and show retrieval ranks. An empty query browses eligible tools. A semantic query reports an explicit error when the model is missing or the relevant active index is incomplete.

Sign in as the platform administrator and open **Evaluations**. Choose a catalog size, case count and split, start the job, inspect the comparison and failed examples, and export JSON or CSV. The catalog consists of generated, non-executable benchmark definitions. The measured full-catalog baseline reports serialized schema bytes. Model-provider token counts and LLM workflow comparisons are tracked separately.

```sh
uv run --locked python -m benchmarks.run --definitions 10000 --cases 1000 --split test --output /tmp/toolmesh-m4-retrieval.json.gz
```

[Measured results and methodology](docs/discovery-evaluation.md) · [Evaluation screenshot](docs/screenshots/m4-evaluations.png)

## M3 walkthrough

1. Sign in as `platform-admin`. In **Services**, register **Inventory** or **IT Operations** by choosing its configured target. Use **Test connection** to check the live service.
2. Open **AutoCrawler**, select the service and run a crawl. Jobs and discovered changes survive reload. Payments and Inventory supply OpenAPI, Support and Communication expose native MCP metadata, and IT uses the checked-in Protobuf descriptor after a live gRPC probe.
3. Open a draft from the crawl results. Review its schema and publish it with a reason. The Registry shows versions, schema diffs, and release history. Compatible prior releases can be rolled back after publication.
4. Sign in as `it-agent` with `TOOLMESH_IT_PASSWORD`. In **Playground**, invoke `it.check_service_health` with `{"customer_id":"C-102","service_id":"sandbox-api"}`, or invoke `inventory.get_inventory` with `{"customer_id":"C-102","item_id":"SKU-102"}` after its catalog release. To verify the gRPC write path, create an incident against `sandbox-api` and retrieve it through `it.list_incidents`.

The allowlist accepts configured target IDs. The API resolves their addresses, validates the local network ranges, pins the selected address for the crawler connection and refuses redirects. Direct URL registration and uncontrolled paths are unavailable. Services show the last check and a stale label after five minutes. The M3 inventory fixture is an extensibility demonstration. Its sandbox stock is stored in PostgreSQL.

The integration suite reproduces a breaking schema change by adding a required inventory location on a disposable database. The crawl immediately quarantines the old release. Its replacement remains a draft until explicit publication. The test verifies that the new contract requires `location`, then restores the original fixture behavior. Inspect a job in [AutoCrawler](http://127.0.0.1:5173/crawler).

## M2 walkthrough

1. Sign in to **Registry** as `platform-admin` and publish version 1 of `payments.check_duplicate_payment`, `payments.get_transaction`, `support.create_ticket`, `payments.issue_refund`, `communication.send_notification`, and `support.resolve_ticket`. Add a review reason for each draft.
2. Sign in as `support-agent`. Open **Playground → Open scripted duplicate-payment workflow** and investigate `O-102`. The gateway records the duplicate check and opens a ticket containing evidence for `TX-102-A` and `TX-102-B`. The installment control case `O-103` completes without a ticket.
3. Sign in separately as `finance-agent` using `TOOLMESH_FINANCE_PASSWORD`. Open the saved run, choose `TX-102-B`, and request a refund. The approval binds that capture, its full amount of 4,550 minor units in USD, the customer and the published version.
4. Sign in as `refund-approver` using `TOOLMESH_APPROVER_PASSWORD`. Open the pending request in **Approvals**, review the bound intent and record a decision. The execution ID also identifies the refund ledger entry.
5. Return to the saved run as `support-agent`. After the refund status reaches `SUCCEEDED`, select **Notify customer and resolve ticket**. The service writes one local notification row per refund and resolves the ticket after confirming both the ledger and the notification. **Overview**, **Approvals**, **Audit**, and **Executions** read the recorded outcomes.

The test suites publish tools and consume fixture refund balances. Use identified local sandbox credentials from `.env` and a separate Compose project for a clean walkthrough.

## Single-tool walkthrough

1. Open **Registry** and sign in as `platform-admin` using `TOOLMESH_ADMIN_PASSWORD` from the local environment. Review and publish `payments.check_duplicate_payment` and `support.create_ticket` with a reason. Services reports the resulting published counts.
2. Sign out, then sign in as `support-agent` with `TOOLMESH_SUPPORT_PASSWORD`. In **Playground**, select `payments.check_duplicate_payment` and invoke it with `{"customer_id":"C-102","order_id":"O-102"}`. The actual result identifies `TX-102-A` and `TX-102-B`. `O-103` is the legitimate installments control case.
3. Invoke `support.create_ticket` with `{"customer_id":"C-102","order_id":"O-102","subject":"Duplicate capture investigation"}`. Open **Execution trace** from the result. Once published, `support.search_tickets` and `support.get_ticket` can read the persisted ticket.
4. To exercise restricted discovery, sign in as `payments-agent` with `TOOLMESH_PAYMENTS_PASSWORD`. Support tools and other principals’ traces are outside this agent’s scope. A direct invocation requesting another customer returns a persisted denial.

The MCP Streamable HTTP endpoint is `http://127.0.0.1:8000/mcp/`. Obtain a one-hour bearer token from `POST /api/v1/auth/login` with `username` and `password`. The native client in `tests/test_gateway_slice.py` demonstrates initialization, listing the four meta-tools, discovery, schema retrieval and invocation with that bearer token.

### Current scope

The sandbox implements five services and up to fourteen gateway business operations, including the supplemental Support `get_ticket` and the inventory fixture. The IT slice covers health, incident creation and incident listing. The guided flow is a persisted, deterministic state machine. It uses real gateway calls, with separate signed-in principals for each handoff. Model-driven LangGraph runs and broader agent profiles come in later milestones. Approved refunds with an uncertain outcome are reconciled by reading the Payments ledger. If the ledger has no matching entry, the execution stays `UNKNOWN` for review.

### Reproduce a timeout after commit

Use a disposable Compose project. This test-only switch delays the Payments HTTP response after its ledger commit. The gateway reaches its deadline, reports `UNKNOWN`, and later reconciles the exact confirmed ledger entry.

```sh
TOOLMESH_TEST_REFUND_DELAY=1 API_PORT=18002 WEB_PORT=15175 docker compose -p toolmesh-m2-fault up --build -d --wait
TOOLMESH_TEST_REFUND_DELAY=1 TOOLMESH_TEST_API=http://127.0.0.1:18002 uv run --locked pytest -q tests/test_refund_workflow.py -k duplicate_refund
API_PORT=18002 WEB_PORT=15175 docker compose -p toolmesh-m2-fault down
```

For the full fresh-sandbox browser sequence, run `TOOLMESH_EXPECT_DRAFT=1 npm --prefix apps/web run test:e2e -- overview.spec.ts workspace.spec.ts`, then `npm --prefix apps/web run test:e2e -- refund.spec.ts crawler.spec.ts`. The opt-in `TOOLMESH_TEST_MUTATE_POLICY=1` pytest flag exercises expiry and grant revocation using the disposable project's database. Set `TOOLMESH_TEST_COMPOSE_PROJECT` if using a project name other than `toolmesh`.

The screenshots show real sandbox data and decisions.

[Crawler run](docs/screenshots/m3-crawler.png) · [Completed workflow](docs/screenshots/m2-workflow.png) · [Approval](docs/screenshots/m2-approval.png) · [Registry](docs/screenshots/m1-registry.png) · [Architecture](docs/architecture.md) · [Threat model](docs/threat-model.md) · [Changelog](CHANGELOG.md)
