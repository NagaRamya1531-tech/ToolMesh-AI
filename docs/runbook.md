# Sandbox runbook

These walkthroughs use the local Compose sandbox and seeded synthetic data. Follow the [README setup](../README.md#quick-start) first. Test runs publish contracts and consume fixture refund balances. Use an isolated Compose project for a clean demonstration.

## Register, crawl, and publish a service

1. Sign in as `platform-admin`. In **Services**, register **Inventory** or **IT Operations** by choosing its configured target. Use **Test connection** to check the live service.
2. Open **AutoCrawler**, select the service and run a crawl. Jobs and discovered changes survive reload. Payments and Inventory supply OpenAPI, Support and Communication expose native MCP metadata, and IT uses the checked-in Protobuf descriptor after a live gRPC probe.
3. Open a draft from the crawl results. Review its schema and publish it with a reason. The Registry shows versions, schema diffs, and release history. Compatible prior releases can be rolled back after publication.
4. Sign in as `it-agent` with `TOOLMESH_IT_PASSWORD`. In **Playground**, invoke `it.check_service_health` with `{"customer_id":"C-102","service_id":"sandbox-api"}`, or invoke `inventory.get_inventory` with `{"customer_id":"C-102","item_id":"SKU-102"}` after its catalog release. To verify the gRPC write path, create an incident against `sandbox-api` and retrieve it through `it.list_incidents`.

The allowlist accepts configured target IDs. The API resolves their addresses, validates the local network ranges, pins the selected address for the crawler connection and refuses redirects. Direct URL registration and uncontrolled paths are unavailable. Services show the last check and a stale label after five minutes. The M3 inventory fixture is an extensibility demonstration. Its sandbox stock is stored in PostgreSQL.

The integration suite reproduces a breaking schema change by adding a required inventory location on a disposable database. The crawl immediately quarantines the old release. Its replacement remains a draft until explicit publication. The test verifies that the new contract requires `location`, then restores the original fixture behavior. Inspect a job in [AutoCrawler](http://127.0.0.1:5173/crawler).

## Run the duplicate-payment workflow

1. Sign in to **Registry** as `platform-admin` and publish version 1 of `payments.check_duplicate_payment`, `payments.get_transaction`, `support.create_ticket`, `payments.issue_refund`, `communication.send_notification`, and `support.resolve_ticket`. Add a review reason for each draft.
2. Sign in as `support-agent`. Open **Playground → Open scripted duplicate-payment workflow** and investigate `O-102`. The gateway records the duplicate check and opens a ticket containing evidence for `TX-102-A` and `TX-102-B`. The installment control case `O-103` completes without a ticket.
3. Sign in separately as `finance-agent` using `TOOLMESH_FINANCE_PASSWORD`. Open the saved run, choose `TX-102-B`, and request a refund. The approval binds that capture, its full amount of 4,550 minor units in USD, the customer and the published version.
4. Sign in as `refund-approver` using `TOOLMESH_APPROVER_PASSWORD`. Open the pending request in **Approvals**, review the bound intent and record a decision. The execution ID also identifies the refund ledger entry.
5. Return to the saved run as `support-agent`. After the refund status reaches `SUCCEEDED`, select **Notify customer and resolve ticket**. The service writes one local notification row per refund and resolves the ticket after confirming both the ledger and the notification. **Overview**, **Approvals**, **Audit**, and **Executions** read the recorded outcomes.

The test suites publish tools and consume fixture refund balances. Use identified local sandbox credentials from `.env` and a separate Compose project for a clean walkthrough.

## Invoke a single tool

1. Open **Registry** and sign in as `platform-admin` using `TOOLMESH_ADMIN_PASSWORD` from the local environment. Review and publish `payments.check_duplicate_payment` and `support.create_ticket` with a reason. Services reports the resulting published counts.
2. Sign out, then sign in as `support-agent` with `TOOLMESH_SUPPORT_PASSWORD`. In **Playground**, select `payments.check_duplicate_payment` and invoke it with `{"customer_id":"C-102","order_id":"O-102"}`. The actual result identifies `TX-102-A` and `TX-102-B`. `O-103` is the legitimate installments control case.
3. Invoke `support.create_ticket` with `{"customer_id":"C-102","order_id":"O-102","subject":"Duplicate capture investigation"}`. Open **Execution trace** from the result. Once published, `support.search_tickets` and `support.get_ticket` can read the persisted ticket.
4. To exercise restricted discovery, sign in as `payments-agent` with `TOOLMESH_PAYMENTS_PASSWORD`. Support tools and other principals’ traces are outside this agent’s scope. A direct invocation requesting another customer returns a persisted denial.

The MCP Streamable HTTP endpoint is `http://127.0.0.1:8000/mcp/`. Obtain a one-hour bearer token from `POST /api/v1/auth/login` with `username` and `password`. The native client in `tests/test_gateway_slice.py` demonstrates initialization, listing the four meta-tools, discovery, schema retrieval and invocation with that bearer token.

## Diagnose startup and dependency failures

- Use `docker compose ps -a` and `docker compose logs postgres migrate api web` to locate a bootstrap failure. The one-shot migration service must exit successfully.
- `GET /healthz` reports process liveness. `GET /readyz` returns HTTP 503 if PostgreSQL, pgvector, the expected Alembic revision, the workspace or Redis is unavailable. An API that is alive may still reject operations.
- On a disposable stack, run `docker compose stop redis` to exercise the quota failure path. Check that liveness stays HTTP 200 and readiness reports HTTP 503. Run `docker compose up -d --wait redis api` to restore service.
- If initialization left a partial volume, use a separate Compose project with unused host ports. Preserve the original data while diagnosing it.
- If the semantic model is unavailable, check the read-only model directory, re-run the pinned download command, then restart the API. Keyword discovery remains available.

## Reproduce a timeout after refund commit

The disposable Payments service delays its HTTP response after the ledger commit. The gateway records `UNKNOWN`. Its reconciler reads and verifies the exact receipt before changing the execution to `SUCCEEDED`.

```sh
TOOLMESH_TEST_REFUND_DELAY=1 API_PORT=18002 WEB_PORT=15175 docker compose -p toolmesh-m2-fault up --build -d --wait
TOOLMESH_TEST_REFUND_DELAY=1 TOOLMESH_TEST_API=http://127.0.0.1:18002 uv run --locked pytest -q tests/test_refund_workflow.py -k duplicate_refund
API_PORT=18002 WEB_PORT=15175 docker compose -p toolmesh-m2-fault down
```

An absent receipt leaves the refund execution uncertain. Ordinary writes do not use the refund reconciler. Inspect the execution and domain records before any manual follow-up.

## Reproduce an evaluation

[Discovery evaluation methodology and raw results](discovery-evaluation.md) documents the pinned model, dataset split, expected files and exact commands. Evaluation runs are administrator-only and use an isolated synthetic corpus.

## Run scripted agent scenarios

Publish the relevant versioned contracts in Registry before running a scenario. Crawl IT from AutoCrawler if its original health and incident contracts have not been discovered.

| Sandbox identity | Scenario |
|---|---|
| `support-agent` | Read the customer and transactions, then inspect duplicate evidence for `O-102`. Use the separate duplicate-payment workflow for the Finance and approver handoff. |
| `finance-agent` | Compare the seeded November 2026 and October 2026 periods and persist a report. |
| `it-agent` | Inspect `sandbox-api` health and ERROR logs, record an incident, and wait for restart approval. |
| `hr-agent` | Request future leave for `E-102` and record a simulated notification after confirmation. |

Open **Playground → Agent scenarios**, enter the requested arguments, and inspect the ordered execution links. For the IT run, sign in separately as `refund-approver`, approve the restart in **Approvals**, then return as the initiating agent and select **Resume after decision**. The final health step verifies the persisted simulated state.

A platform administrator can create an agent in **Agents**, configure an operation-specific rule and run **Simulate** in **Policies**, or update the quotas in **Settings**. Simulation checks access and object scope without invoking a tool.
