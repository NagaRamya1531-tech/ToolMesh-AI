# Changelog

## M4 · Ranked discovery and retrieval evaluation

- Added authorization-prefiltered PostgreSQL full-text search, pgvector cosine ranking with a pinned CPU model, and reciprocal rank fusion in REST, MCP and Playground tool discovery.
- Added a reproducible synthetic retrieval dataset with disjoint tuning and test scope groups. Durable administrator-only evaluation runs report progress, comparative and per-domain results, missed cases, negative-case returns, and downloadable JSON and CSV.
- Published the raw 10,000-definition, 1,000-case held-out run. Measured Recall@5 was 71.2% for keyword, 81.7% for vector and 89.5% for hybrid. Every strategy returned candidates for all negative cases. Method, measured SQL timings, and limitations appear in [the evaluation report](docs/discovery-evaluation.md).

### Verification

- Applied migration `0006` to the existing sandbox and repeated the migration on a fresh stack. Thirteen Python checks and nine Chromium checks passed, including scoped ranking, hybrid Playground search and evaluation export.
- The first local semantic smoke test detected a model dimension check that counted the serialized vector string. The check now validates the vector dimensions, and real inference and evaluation passed on rerun.
- The secret scan identified a public pinned tokenizer checksum. The allowlist covers only that checksum in the embedding module. Local lint, formatting, type checks and the production frontend build passed.

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
