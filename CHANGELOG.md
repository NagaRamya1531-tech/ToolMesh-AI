# Changelog

## M8 · OpenAI Playground and architecture refresh

- Extended the console redesign to all sidebar destinations. Added section headers, status labels, responsive record inspectors, and side-by-side setup and history for Agents and Evaluations. Browser coverage verified route controls, selected-record highlighting, and the mobile drawer. The mobile Registry overflow found on the first pass was corrected.

- Redesigned the console shell and Overview with a tool request path, dependency status cards, and outcome breakdown. The activity cards and chart link to scoped execution and approval views. Verified the production build, the signed-in time-range drill-down, and six Chromium checks including axe and responsive readiness.

- Added the opt-in OpenAI Playground for one scoped sandbox read per task. The console shows the tool result, execution trace, provider-reported tokens and estimated cost.
- Bound provider input, candidates, request deadline and output tokens. Reuse gateway quota, session, scope, version, schema and execution checks. Keep the server credential outside the console.
- Refreshed the console palette, README, and architecture diagrams. Added a standalone SVG, an ADR, and live desktop and mobile screenshots.
- Four profile cases and all 16 corrected benchmark trials passed with GPT-5.4 mini. The first live browser scan found a keyboard issue in scrollable results. The fixed panel passed its rerun. Estimated list-rate cost across all 21 validation requests was $0.0090. [Method and raw evidence](docs/release-validation.md#m8-corrected-provider-validation)

- Twenty-five Python tests, twelve standard Chromium tests and one live OpenAI Chromium test passed. Verified lint, formatting, types, frontend production build, local Mermaid rendering, documentation links and secret scanning.

## M7 · Sandbox release v0.1.0

- Added a loopback Ollama evaluation harness comparing upfront schemas, keyword, vector and hybrid discovery. The runner verifies selected fixture reads through the live gateway and records provider-reported usage.
- Published a held-out 1,000-definition retrieval run, four model tasks per strategy, and a 100-client authenticated discovery burst. Hybrid Recall@5 was 94.4%, ranked model trials verified two of four tasks per strategy, and the burst returned 100 HTTP 200 responses. [Method, raw evidence and limitations](docs/release-validation.md)
- Updated the demo, README, security and architecture notes, ADRs and a real console evaluation screenshot.

The OpenAI follow-up completed 16 GPT-5.4 mini requests for an estimated $0.01025. It verified three reads per strategy and abstained on the missing IT operation. The runner now crawls and verifies the scoped catalog before making provider requests. The corrected IT read was verified through gRPC. [Follow-up results](docs/release-validation.md#optional-openai-comparison)

### Verification

- Built and bootstrapped a fresh clone, repeated migration `0009`, and exercised REST, MCP and gRPC against real dependencies. Eleven Chromium checks passed, including keyboard operation and axe checks at the documented viewport sizes.
- In the delayed-refund rehearsal, concurrent approval produced one logical effect. The execution returned `UNKNOWN` before receipt reconciliation, then reached `SUCCEEDED`. PostgreSQL contained one refund for the selected transaction.
- Retained the initial failed provider comparison. A wrong model asset path affected the first browser evaluation checks and was corrected for the rerun. The combined fault-injected Python run exposed an immediate-response test incompatible with injected delay. The standard suite passed all 19 tests on a separate fresh stack with Python 3.12 and injection disabled.

## M6 · Reliability and operations

- Added PostgreSQL execution timelines and request and OpenTelemetry correlation IDs. The scoped REST endpoint, console and operator CLI expose the recorded evidence.
- Added Prometheus request and execution metrics, approval metrics, bounded read retries, and per-service circuit breakers. Stale ordinary reads are classified as failed, and uncertain writes are surfaced for review. Existing approval receipt reconciliation remains active.
- Added a mobile execution table layout, real desktop and mobile screenshots, an operational rehearsal and raw evidence, and updated the architecture diagrams, ADR and runbook.

### Verification

- Applied and repeated migration `0009`. Nineteen Python tests and eleven Chromium tests passed against Compose, including execution event visibility and mobile accessibility. A loopback OTLP receiver decoded an exported trace and confirmed that request path and query input were omitted.
- The isolated rehearsal completed 20 reads at concurrency four, verified retry and circuit counters, recovered after restoring Payments, and denied invocation during Redis failure. Seeded stale reads became failed and writes remained uncertain. [Method and raw evidence](docs/operations-rehearsal.md)
- Early local attempts exposed fixture targeting and quota-window interference across suites and degraded timings during simultaneous stack startup. Verification used isolated Compose projects and fresh quota state. Lint, format, type checks, the production frontend build, secret scan, link checks and local Mermaid rendering passed.

## M5 · Domain breadth and governance

- Completed all 36 specified operations across Payments, Support, IT Operations, HR, Analytics, and Communication. Added masked payment methods, ticket history, simulated restart receipts, HR allowance and leave records, read-only Payments projections for Analytics, and local messaging.
- Added the Support, Finance, IT, and HR scripted LangGraph profiles with persisted execution steps and approval resumption for IT restarts.
- Added managed agent accounts, session revocation, versioned principal policy rules, access simulation, configurable Redis quotas, and credential-pattern redaction on projected tool results.
- Updated the README, architecture and threat model, runbook, ADR index, and console screenshots.

### Verification

- Applied the domain and agent-run migrations through `0008` on a fresh Compose stack and repeated the migration successfully.
- Fifteen Python checks and eleven Chromium checks passed. The domain suite exercised all 36 required operations, restart approval and health verification, leave balance and overlap rules, report persistence, policy precedence and history, quota responses, and session revocation.
- Confirmed that Analytics can read its exported Payments projection, its role cannot update Payments transactions, and the gateway role cannot read HR employee records.
- Early runs exposed a fresh-database ordering issue for the IT service row, a class-method name that shadowed a Python type, and a browser check that raced service loading. Fixed each and reran verification.
- Locked dependency sync, lint, formatting, type checks, frontend build, secret scan, Mermaid rendering, and repository link checks passed.

## M4 · Ranked discovery and retrieval evaluation

- Added authorization-prefiltered PostgreSQL full-text search, pgvector cosine ranking with a pinned CPU model, and reciprocal rank fusion in REST, MCP and Playground tool discovery.
- Added a reproducible synthetic retrieval dataset with disjoint tuning and test scope groups. Durable administrator-only evaluation runs report progress, comparative and per-domain results, missed cases, negative-case returns, and downloadable JSON and CSV.
- Published the raw 10,000-definition, 1,000-case held-out run. Measured Recall@5 was 71.2% for keyword, 81.7% for vector and 89.5% for hybrid. Every strategy returned candidates for all negative cases. Method, measured SQL timings, and limitations appear in [the evaluation report](docs/discovery-evaluation.md).

### Verification

- Applied migration `0006` to the existing sandbox and repeated the migration on a fresh stack. Thirteen Python checks and nine Chromium checks passed, including scoped ranking, hybrid Playground search and evaluation export.
- The first local semantic smoke test detected a model dimension check that counted the serialized vector string. The check now validates the vector dimensions, and real inference and evaluation passed on rerun.
- The secret scan identified a public pinned tokenizer checksum. The allowlist covers only that checksum in the embedding module. Local lint, formatting, type checks and the production frontend build passed.

### Documentation follow-up

- Reorganized the README around problem, architecture, operations, measurement, and roadmap. Added the runbook, ADR index, and milestone documentation checklist.
- Added Mermaid diagrams for the control and data planes, discovery, execution, approvals, version lifecycle, database relationships, recovery, and Compose deployment.
- Rendered all 11 diagrams with Mermaid CLI 10.9.1 and 11.17.1, verified relative links and anchors, and checked the README benchmark table against the stored M4 export.

## M3 · AutoCrawler, release lifecycle and gRPC IT

- Added allowlisted service registration, editable display names, live connection checks and persisted health observations.
- Added durable crawl jobs for OpenAPI, native MCP and checked-in Protobuf descriptors. The console shows job history, unsupported operations, drafts and schema diffs.
- Added version review, publication, disable, archive and compatible rollback. Breaking or missing source contracts quarantine the active release and create release-history evidence.
- Added the Inventory OpenAPI fixture and three persisted IT operations over gRPC. Public-host registration and gRPC reflection remain outside this milestone.

### Verification

- Upgraded the existing sandbox and started a fresh acceptance stack. Alembic revision `0005` and a repeat migration succeeded.
- Eleven Python checks and seven Chromium checks passed. Coverage includes allowlist rejection, OpenAPI, native MCP and Protobuf discovery across five targets, browser registration and publication, gRPC health and persisted incident operations, compatible rollback and breaking inventory schema quarantine.
- The inventory test added a required location, observed a denied invocation before replacement publication, then verified the new contract against a persisted stock record. The fixture was restored at the end of the test.
- Reproduced the checked-in Protobuf artifacts with the locked compiler. Local lint, formatting, type checks and the frontend build passed.

## M2 · Approval-gated duplicate-payment workflow

- Added a durable guided workflow with structured duplicate evidence, separate Support and Finance handoffs, human approval and persisted completion.
- Added the Payments refund ledger and receipt lookup, canonical intent binding, expiry, grant and publication rechecks, scoped idempotency, and a read-only reconciliation worker.
- Added the native Communication MCP outbox, one simulated notification per refund, and guarded Support ticket resolution.
- Added Overview activity, Approvals, Audit and CSV export with real persisted data. Later model-driven agent runs and recovery of unproven writes remain out of this milestone.

### Verification

- Upgraded the M1 sandbox and bootstrapped a separate clean stack. Alembic revision `0004` and a repeat migration succeeded.
- Ten Python checks and six Chromium checks passed on the clean acceptance stack. Browser coverage includes publishing, the Support-to-Finance handoff, a separate approver, and confirmed notification and resolution.
- Verified denials for Support refund attempts, self-approval, administrator approval without the approver grant, expiry, revocation, early notification, and early resolution. Rejected and revoked requests produced no refunds.
- The separate fault stack delayed the Payments response after commit. The gateway recorded `UNKNOWN`, then reconciled to `SUCCEEDED`. PostgreSQL contained exactly one refund row for `TX-102-B` for 4,550 USD minor units.
- Early integration runs found a Postgres row-lock privilege error and strict UUID parsing at the Payments HTTP boundary. The gateway now keeps the grant table read-only, and the refund lookup accepts JSON UUID strings. Reruns passed.
- Lint, formatting, type checks, frontend build and the local secret scan passed. Remote GitHub Actions still awaits its first run.

## M1 · Authenticated MCP vertical slice

- Added three Payments REST operations and three native Support MCP operations with Postgres-backed sandbox data. The gateway and both domain services use distinct PostgreSQL roles. `get_ticket` supplements the SRS operation list.
- Added opaque one-hour sessions, revocation, service and customer grants, Redis quotas, and bounded requests and upstream responses.
- Added immutable catalog drafts, manual publication with a recorded reason, MCP Streamable HTTP and four gateway meta-tools.
- Added authenticated Services, Registry, Playground and Executions routes. Execution records preserve actor, version, outcome and timings.

### Verification

- Upgraded the existing M0 volume and bootstrapped an isolated fresh stack. Revision `0002` applied and a second upgrade completed successfully.
- Eight Python checks and five Chromium checks passed. The Python suite used a FastMCP client against the running gateway and exercised all six operations. Browser verification required a draft, published it, switched identity, invoked it and opened its trace.
- Verified that the gateway role cannot read Payments or write Support and that the two domain roles cannot read each other's tables.
- Stopped Redis during a Support write. The gateway persisted `DENIED` with `QUOTA_UNAVAILABLE`, the ticket count stayed constant and readiness recovered after Redis restarted.
- Fixed two integration failures found on early runs. PostgreSQL needed explicit parameter types in role provisioning, and FastMCP passed a redirect option to the bounded HTTP client factory. Early browser locators also needed to target the actual combobox and trace details.
- Lint, formatting, type checks, the frontend build and local secret scan passed. The frontend build still emits client-directive warnings. GitHub Actions awaits its first remote run.

## M0 · Foundation

- Added a FastAPI application with bounded Postgres and Redis readiness probes.
- Added the Compose sandbox, least-privilege runtime login, pgvector and the initial Alembic migration.
- Added a responsive React console with live readiness, refresh and retry.
- Added Python and npm lockfiles, local verification steps and GitHub Actions gates.

### Verification

- Fresh Compose bootstrap and repeat migration succeeded on macOS with Docker Engine 29.7.2. PostgreSQL reported pgvector 0.8.2 and Alembic revision `0001`. The runtime role could read the workspace and lacked write and schema-creation grants.
- Five Python tests and four Chromium tests passed. Browser checks covered 1440, 1024 and 390 px, sidebar and keyboard navigation, automated axe checks and retry after a failed request.
- Stopping Redis left liveness at HTTP 200 and changed API and console readiness to HTTP 503. Restarting Redis restored readiness.
- The first Postgres bootstrap failed while executing a bind-mounted initialization script on macOS. The Postgres image now includes the script, and the default project was verified on a fresh volume.
- Local lint, formatting, type checks, frontend build and secret scan passed. The frontend build emitted warnings about TanStack Query's client directives. The GitHub Actions workflow awaits its first remote run.
