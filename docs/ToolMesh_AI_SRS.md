# ToolMesh AI | Engineering specification

**Version 1.0 · Proposed implementation · 18 November 2026**

**Product:** Enterprise MCP gateway, service catalog and agent execution control plane.

**Purpose:** Build a production-style, independently implemented infrastructure platform inspired by the incremental tool-discovery approach described in Uber's public MCP Gateway material. This is not Uber's internal implementation or a claim of production parity.

## 01 / Product definition

### Problem and users
Enterprise agents need to use many APIs without loading every tool schema into an LLM, creating bespoke integrations, or relying on a model to enforce permissions. ToolMesh provides a shared registry, secure execution gateway, actionable operations console and reproducible evaluation harness.

- Platform administrator: registers services, configures policies, manages versions and diagnoses failures.
- Service owner: reviews discovered tools, approves publication and maintains schemas.
- Agent developer: configures identities, discovers tools, tests workflows and inspects traces.
- Approver: authorizes or rejects specific sensitive operations within assigned scope.
- Auditor: reads redacted execution evidence and policy decisions; cannot mutate resources.

### Scope and non-goals
- Full local target: 6 independent domain services, 36 working business tools, 4 gateway meta-tools, 4 agent profiles, REST/gRPC/native MCP adapters and an administrative web app.
- Benchmark-only catalog: up to 10,000 generated definitions, indexed and labeled separately; non-executable unless explicitly backed by a real implementation.
- Simulated payments, infrastructure and workforce operations backed by deterministic persistent data. No external money movement, real employee data, production infrastructure changes or actual outbound email by default.
- Production-style engineering, not a production-certified system: local Docker Compose first; cloud deployment, enterprise SSO, HA and third-party connectors are later extensions.

### Success criteria
A reviewer can clone, bootstrap, sign in, discover a service, publish an approved tool, run a cross-service workflow, observe a denied action, approve a simulated refund, inspect the persisted trace, and reproduce benchmark results without manually editing the database. No UI action silently succeeds, no invented metrics, and no mock-only primary feature is represented as complete.

### Delivery contract
Build in vertical slices; each slice includes real persistence, authorization, UI where relevant, tests and documentation. M0–M2 are the initial MVP; M3–M7 extend it. All numeric goals in this specification are targets, not measured results.

---

## 02 / System architecture

### Runtime and administrative boundaries
```
Browser / CLI / MCP clients / LangGraph agents
        | HTTPS / Streamable HTTP MCP
        +--> Control API: registry, policies, approvals, crawler
        +--> Runtime gateway: discovery, schema, invocation
                     | policy + version + validation + redaction
                     +--> REST adapter --> Payments / HR / Analytics
                     +--> MCP client ----> Support / Communication
                     +--> gRPC adapter --> IT Operations
        PostgreSQL + pgvector | Redis | OTel / Prometheus
```

Control plane owns mutable catalog metadata, role grants, approval state, release history and crawl jobs. Runtime consumes approved versions and fails closed on unresolved authorization. Both can start as separate modules in one deployable API; split processes only when justified and document the boundary. Gateway is a real MCP server, not just REST endpoints named after MCP methods. Connect via a compatible MCP SDK and Streamable HTTP; use native tool listing plus invocation behind the proxy. Avoid arbitrary code execution.

### Chosen stack
| Layer | Implementation |
|---|---|
| Runtime/control | Python 3.12+, FastAPI, FastMCP, Pydantic, async HTTPX |
| Agent runner | LangGraph; pluggable LLM provider, deterministic scripted test runner |
| State | PostgreSQL + pgvector; SQLAlchemy and Alembic; Redis for quotas/cache |
| Integration | OpenAPI, native MCP, grpcio with checked-in Protobuf descriptors |
| UI | React + TypeScript + Vite, Tailwind, accessible components, TanStack Query/Table, Recharts |
| Operations | OpenTelemetry, structured logs, Prometheus; optional Grafana profile |
| Quality | pytest, React Testing Library, Playwright, Locust, GitHub Actions, uv, Docker Compose |

Pin verified compatible versions using lockfiles. Prefer CPU-friendly embeddings (BGE-small or an evaluated equivalent); model download is explicit. No GPU required for the local baseline. Use a dev-only deterministic embedder only for tests and mark it as such. All servers and UI use health checks and configurable ports. Secrets live in environment/secret storage, never git or registry responses.

### Design constraints
API calls are bounded by deadlines and response size; adapters normalize errors. Per-tool schema versions are immutable. Use UTC timestamps, UUID IDs, stable tool namespaces and request/trace IDs. Use database transactions for policy/approval state, and durable pending execution records before side effects. Scope a default workspace; if multiple workspaces are introduced, enforce isolation across every read and write.

---

## 03 / Domain services and actual tools

Each service owns its persistence and domain rules; the gateway cannot directly edit domain tables. Separate databases or isolated schemas/users are acceptable locally. All business tools have validated schemas, deterministic seed data, stable error codes, realistic read/write behavior and contract tests. Monetary values use integer minor units and currency codes.

| Service / protocol | Six functional operations |
|---|---|
| Payments / REST | get_transaction; list_transactions; check_duplicate_payment; issue_refund; get_refund_status; get_payment_method |
| Customer support / MCP | get_customer; create_ticket; update_ticket; search_tickets; resolve_ticket; escalate_ticket |
| IT operations / gRPC | check_service_health; get_system_logs; list_incidents; restart_service; create_incident; get_deployment_status |
| Human resources / REST | get_employee; get_department; get_leave_balance; request_leave; get_manager; get_company_policy |
| Analytics / REST | get_daily_revenue; get_sales_metrics; get_customer_metrics; generate_report; get_refund_metrics; compare_periods |
| Communication / MCP | send_email; send_notification; search_messages; get_message; create_channel; get_channel_members |

### Domain rules, not dummy responses
Payments: transaction IDs, order IDs, status, currency and captured/refunded amounts; duplicate checks distinguish two captures for the same order from legitimate installments. Refunds cannot exceed available captured balance; unique idempotency key and durable refund ledger. Payment method returns masked metadata only.

Support: tickets persist with history; transition open → investigating → resolved (or escalation); prohibit invalid transitions, support pagination and linked evidence. IT: restart only simulated service state; incidents persist; logs are timestamped and filterable; never execute shell commands. HR: leave requests persist, reject invalid dates or insufficient balances, protect personal records. Analytics: compute aggregates from persisted domain data via read-only APIs/exported fixtures rather than random metrics. Communication: persist messages and notification outbox locally, simulate delivery; no external sends in default mode.

### Data strategy
Deterministic seed (e.g., 42) with small default fixtures and optional scale profile: 10k customers, 100k transactions, 20k tickets, 2k employees, 10k incidents, 100k logs, 50k messages; benchmark definitions up to 10k and 1k labeled queries. Include expected edge-case fixtures: duplicates, partial refunds, disputed payments, outages, permission denials and invalid leave. Synthetic data must be clearly labeled; avoid authentic personal data. Use repeatable seeding and document whether seed is destructive. Never reset non-demo data automatically.

---

## 04 / Registry and discovery requirements

| ID | Functional requirement and acceptance |
|---|---|
| REG-01 | Register service with display name, protocol, owner, allowlisted endpoint, auth-secret reference and health status. Duplicate identifiers rejected; list/detail persist after restart. |
| REG-02 | Run a background crawl, expose job status/diff and error details. REST uses declared OpenAPI; gRPC uses known descriptors/reflection when enabled; MCP uses tools/list. Generated schemas remain drafts. |
| REG-03 | Validate schemas against supported JSON Schema subset; unsupported endpoints are flagged, never silently exposed. Version diff includes added/removed fields and required-field changes. |
| REG-04 | Review, publish, disable, archive and roll back approved compatible versions. Record actor/reason/time; invalid transitions rejected; disable affects new calls. |
| REG-05 | Health checks with last-checked time and error; dashboard distinguishes healthy, degraded, unreachable and unknown; stale health is visible. |
| DIS-01 | discover_server(query, limit): return authorized server IDs, summaries and ranks; never expose restricted services. |
| DIS-02 | discover_tools(server_id, query?, limit): return allowed published tool IDs, brief descriptions, version and risk; avoid full schemas. |
| DIS-03 | get_tool_schema(tool_id, version?): return permitted immutable input/output contract, limits and approval metadata; reject unavailable versions. |
| DIS-04 | invoke_tool(tool_id, arguments, version?, idempotency_key?, fields?): execute only a permitted published version and return structured success/error/pending result. Caller identity derives from auth, never model-supplied arguments. |
| DIS-05 | Compare keyword, vector and hybrid discovery; index published versions and prefilter by authorization. Use reciprocal rank fusion; bound top-K and query length. Log strategy and rank without leaking denied tools. |

### Lifecycle and error handling
Draft → review → published → disabled/retired. Versions are immutable; approval to publish a schema is distinct from approval to execute a sensitive action. Recheck policy and current active version at execution (including resumption); breaking schema drift disables or quarantines the affected tool until reconciled. Use PostgreSQL source of truth and cache invalidation; do not allow stale allow decisions after revocation. Use explicit error codes such as INVALID_ARGUMENT, TOOL_NOT_AVAILABLE, RATE_LIMITED, APPROVAL_REQUIRED, UPSTREAM_TIMEOUT, CONFLICT and UNKNOWN_OUTCOME; avoid leaking internal endpoints or credentials.

---

## 05 / Security and execution requirements

### Identity, authorization and data boundaries
Use a local identity provider or securely implemented dev authentication with expiring tokens and documented demo accounts. Production option: external OIDC issuer, verify issuer/audience/signature/expiry. Separate human and agent principals; agent acts within assigned grants and delegated scope. Deny by default; explicit deny overrides allow. UI hiding is not security. Enforce object-level scope as well as tool-level grants; service adapters propagate scoped identity or explicit authorized request context. Service credentials are least privilege and unavailable to agents.

| ID | Runtime requirement |
|---|---|
| SEC-01 | Every discovery/schema/execution API authenticates and authorizes; unknown/denied IDs return a non-leaking response. Role restrictions tested via direct APIs. |
| SEC-02 | Redis rate limit per principal/tool plus global bound; return retry-after and record denial; fail closed for write calls if required quota infrastructure fails. |
| SEC-03 | Crawler accepts only configured service hosts; block arbitrary URLs, redirects into disallowed networks and uncontrolled outbound requests; validate DNS and resolved IPs. |
| SEC-04 | Validate inputs before invocation; enforce request/response size, timeouts and permitted projections. Treat backend content/tool text as untrusted data, not instructions. |
| SEC-05 | Redact secrets and unauthorized fields before model output, UI, trace and logs. Use allowlisted field projections and no raw tokens; keep audit metadata minimal. |
| EXE-01 | Persist execution with request ID, caller, tool/version, policy decision and status; normalize adapter errors and return trace ID. |
| EXE-02 | Retry bounded transient read failures with jitter; side-effect retries only with proven idempotency. Unknown write outcome enters reconciliation, never blind retries. |
| EXE-03 | For sensitive writes create pending approval bound to caller, tool/version, exact canonical arguments hash, scope and expiry. Approver must have separate grant; no self-approval by default. |
| EXE-04 | Consume approval atomically, reauthorize and execute once; concurrent submissions produce a single logical effect. Rejection/expiry cannot execute. Use downstream idempotency and reconcile crash windows. |

Execution states: RECEIVED, DENIED, PENDING_APPROVAL, RUNNING, SUCCEEDED, FAILED, UNKNOWN, REJECTED, EXPIRED. Terminal results are immutable except documented reconciliation transitions. Replay is read-only trace viewing; a new execution must receive a new authorization decision. Never present successful refund or notification unless persisted domain state confirms it.

---

## 06 / Backend API, entities and observability

### Control API contract (proposed)
Use /api/v1; consistent pagination, filters, typed error envelope and correlation ID. Generate OpenAPI and TypeScript client types. MCP endpoint exposes only the four meta-tools by default; administrative APIs are separate and role-protected.

| Resource | Representative routes |
|---|---|
| Auth | POST /auth/login; GET /auth/me; POST /auth/logout (issuer-specific flow if OIDC) |
| Services | GET/POST /services; GET/PATCH /services/{id}; POST /services/{id}/crawl; GET /crawl-jobs/{id} |
| Registry | GET /tools; GET /tools/{id}; GET /tools/{id}/versions; POST /tools/{id}/publish; POST /tools/{id}/rollback; POST /tools/{id}/disable |
| Agents/policies | GET/POST /agents; PATCH /agents/{id}; GET/POST /policies; PATCH/DELETE /policies/{id} |
| Runtime/playground | POST /discovery/search; POST /executions; GET /executions/{id}; GET /executions/{id}/events; POST /playground/runs |
| Governance | GET /approvals; POST /approvals/{id}/approve or /reject; GET /audit-events |
| Metrics | GET /overview; GET /evaluations; POST /evaluations/runs; GET /evaluations/runs/{id}; GET /healthz; GET /readyz |

### Persistence
Core tables: services, tools, tool_versions, crawl_jobs, principals, roles, grants, policy_versions, approvals, executions, execution_events, audit_events, evaluation_runs, evaluation_cases. Domain-specific data separate. Unique constraints on stable qualified tool name, version number and scoped idempotency keys; indexes on status, owner, time and search. Migrations versioned; no runtime destructive sync. Store safe event metadata and correlation IDs, not unrestricted LLM context.

### Observability
OTel spans for discovery, auth, validation, adapter and downstream execution; structured redacted logs; Prometheus counters/histograms for requests, outcome, latency, approval decisions, queue age and cache hits. Avoid high-cardinality metric labels (no user or execution IDs). Dashboard uses aggregated persisted events/metrics; use explicit time range and last refreshed timestamp. Record model usage only when provider reports it, otherwise show unavailable. Collect performance measurements separately for gateway overhead and full model workflow.

---

## 07 / Frontend product and design system

The web application is a real operations console, not a landing page or static dashboard. All screens use typed API data, stable routes, reusable components and permission-aware actions; dummy fixtures are allowed only in component tests or explicitly labeled demo mode. Use a consistent desktop-first shell, responsive tablet/mobile layout and keyboard-accessible navigation.

### Visual direction
- Premium infrastructure-console aesthetic: calm neutral slate background, restrained indigo accent, subtle borders, excellent spacing and readable data density. Default dark theme; accessible light theme optional. No large hero banners, neon gradients, excessive rounded cards or decorative animation.
- Desktop shell: 240px expandable/collapsible sidebar, 56–64px topbar, breadcrumbs, workspace/environment label and user menu. Main content max-width as appropriate; tables use full width. Mobile navigation becomes a drawer. Support 1440px, 1024px and 390px viewport.
- Tokens: 4/8px spacing grid, 6–10px radii, 14px default text, 12px metadata, 24–28px page titles, 32px KPI values; sans-serif UI and monospaced code/IDs. Status colors with labels/icons: success green, warning amber, failure red, neutral gray. Meet WCAG AA contrast; visible focus, reduced motion and no color-only states.
- Data hierarchy: page header with description and primary action; compact filter bar; table or chart; contextual detail drawer. Use skeleton, no-results, error/retry, unauthorized, disabled, stale and offline states. Confirm consequential changes with explanation and typed input only where warranted.

### Navigation and interaction standards
Persistent sidebar groups: Platform (Overview, Services, Registry, AutoCrawler); Access (Agents, Policies, Approvals); Operations (Playground, Executions, Audit); Quality (Evaluations); Settings. Preserve filter/sort/page state in URLs. All table rows link to deep details; copy-safe ID controls, code blocks, search and pagination. Use toast for transient feedback plus persistent status on the record. Disable duplicate submits; mutation invalidates relevant cached data. Do not use random graph data or hardcoded KPI totals.

### Layout blueprint (desktop)
```
+ ToolMesh / workspace ------- search / environment / account +
| PLATFORM    | Overview                         Last 24h v   |
| Overview    | [Services] [Tools] [Calls] [P95] [Pending]    |
| Services    | Execution volume        | Outcome breakdown  |
| Registry    | ----------------------- | ------------------ |
| ...         | Service health          | Pending approvals  |
| OPERATIONS  | Recent executions (filter, sort, deep link)  |
+-------------+-----------------------------------------------+
```

---

## 08 / Frontend screen-level acceptance

| Screen / route | Required working behavior |
|---|---|
| Overview / | Live metrics, time range, health statuses, outcome chart, recent executions, pending approvals; click through to filtered lists. Empty database shows zero/empty, not promotional metrics. |
| Services /services | Search/filter by protocol/health; register/edit service, test connection, trigger crawl; detail shows endpoint (safe), owner, tool list, history and health. |
| Registry /tools | Filter by service/status/risk, keyword search, schema viewer, version selector/diff, review/publish/disable/rollback actions; role checks and success/error states. |
| AutoCrawler /crawler | Create/run crawl from registered service, progress and logs, diff review, discovered drafts, retry failed job; no silent auto-publish. |
| Agents /agents | Create/disable principals, inspect grants, select agent profile; show credential metadata but never reveal stored secrets. |
| Policies /policies | Grant/deny role or principal scopes; review resource/operation restrictions, policy simulation with reason, version history; reject conflicting/invalid input. |
| Approvals /approvals | Pending/approved/rejected/expired tabs; show redacted argument summary, exact intent, requester, risk, expiry and decision reason; approve/reject only when authorized; live status update. |
| Playground /playground | Choose agent/provider, prompt or scripted scenario, stream ordered tool steps and policy outcomes; inspect safe schemas/arguments/results/trace; stop or continue if supported; link pending approval. Clearly label scripted vs LLM mode. |
| Executions /executions | Filter status/tool/agent/date/trace, sortable latency; details show timeline, spans, sanitized arguments/results and error; refresh and safe trace replay, not blind re-execution. |
| Audit /audit | Actor/action/resource/time filters; append-only redacted evidence; export safe CSV for authorized reader; no delete/edit button. |
| Evaluations /evaluations | Launch bounded seeded benchmark, show progress, configuration, results, comparison chart, failed examples and export JSON/CSV. No invented success scores. |
| Settings /settings | Safe workspace/profile defaults, provider availability, quota configuration (authorized only), theme; never return raw API keys; persist supported settings. |

### UI quality gates
Playwright covers navigation and the primary workflow, approval, permission denial and error recovery. Test accessibility (keyboard, labels, contrast), responsive widths, empty and failing API states. Screenshots in documentation must come from running app, clearly labeled with seed/fixture status. For every button in MVP, provide real backend behavior or mark the feature explicitly unavailable with reason; no placeholders masquerading as completed feature.

---

## 09 / End-to-end demo and agent behavior

### Workflow A: duplicate payment investigation (primary)
1. Seed customer C-102, order O-102 and two completed USD 45.50 captures for the same order; include a legitimate nonduplicate control case.
2. Support agent receives complaint; discovers payments, lists transactions and calls duplicate check. Schema fetched only as needed. Show actual evidence and confidence limitations.
3. Support agent creates a ticket linked to the order and both transaction IDs. Support cannot invoke issue_refund directly.
4. Authorized finance agent requests refund on identified duplicate capture. Gateway creates pending approval with exact amount/currency and argument binding. The support agent does not impersonate finance.
5. Authorized approver reviews and approves. Runtime rechecks policy, version and amount; service uses idempotency key. Repeated submission returns existing outcome or conflict, not a second refund.
6. Support agent sees confirmed refund, sends simulated message through communication service and resolves ticket. Trace shows tool calls and outcomes; user can inspect approval/audit and persisted domain records after reload.
7. Negative variants: no duplicate found, approval rejected, downstream timeout after possible commit, revoked permission, sensitive fields requested. Do not report resolution before confirmation.

### Additional workflows
IT: identify simulated service outage, inspect logs, create incident, request approval for restart and verify health after restart. HR: retrieve permitted leave balance, submit valid request and send local notification; unauthorized employee records denied. Finance: retrieve date-bounded revenue/refund metrics, produce report from data and compare periods; enforce authorized scope. These are separate testable workflows, not hardcoded chat answers.

### Agent execution model
Four profiles: support, finance, IT, HR. Use bounded LangGraph state: run ID, principal, user intent, task state, discovered tools, selected version, validated arguments, execution references and approval state. A model selects tools; runtime determines whether calls are allowed. Stop on max steps/deadline, explicit human handoff, ambiguity or terminal error; no infinite tool loops. Tool output is untrusted input. LLM provider is optional for local deterministic demo, but configure and test a real provider separately and label its runs. Persist run state sufficiently to resume waiting workflows safely; never log hidden chain-of-thought.

### Owner/admin demo
Register a new allowlisted fixture service; crawl; inspect draft schema; publish; discover via agent; modify backend schema; see diff; approve replacement or disable incompatible version; verify trace and audit evidence. Demonstrates platform extensibility independently of business workflows.

---

## 10 / Testing, evaluation and reproducibility

| Test suite | Required coverage |
|---|---|
| Unit and contract | Schema parser/adapters, policy precedence, ranking, redaction, state transitions, amount constraints, error mapping, all 36 operation contracts at full scope. |
| Integration | Real PostgreSQL/Redis and backend services; migrations, MCP transport, REST/gRPC routing, crawl → publish → discover → invoke, cache invalidation. |
| Security | Denied discovery/schema/direct invoke, expired/revoked token, object-scope violation, hostile tool text, SSRF controls, projection bypass, approval replay, secret redaction. |
| Resilience | Upstream 429/5xx, timeout, rate limiter outage, stale schema, concurrent approval and idempotent writes, crash/reconciliation, invalid requests. |
| UI and E2E | Playwright primary workflow with actual persisted state, every MVP route, accessible keyboard and responsive layout, backend failure and retries. |

### Evaluation plan
Use a fixed seed and labeled paraphrases/hard negatives, separate tuning/test sets, permission-aware relevance labels, report per-domain results. Compare full upfront schema loading (only where model limits allow), keyword-only discovery, vector-only and hybrid discovery. For oversized full-load baselines report infeasible rather than pretend they ran. Capture initial and total tokens using actual model/provider counters where available; include extra discovery round trips and report task completion, cost and latency. Synthetic catalog experiments separate non-executable retrieval data from real runtime workloads.

| Metric | Target (not achieved) |
|---|---|
| Discovery Recall@5 / MRR | ≥95% Recall@5; report MRR, Precision@5 and misses |
| Gateway overhead | p95 <100 ms on declared local warm workload; exclude LLM/downstream time explicitly |
| Authorized task success | ≥90% on labeled workflow cases; report deterministic and LLM separately |
| Initial context reduction | ≥95% at 1,000 definitions; measure total separately |
| Unauthorized effects / duplicate refunds | Zero in defined adversarial/concurrency suite; not a guarantee of absolute security |
| Load | 100 simulated clients; report p50/p95/p99, throughput, errors, CPU/RAM and dataset size |

Publish raw results, model versions, seed, fixture generator, machine/profile, run commands and limitations. Record failed experiments and regressions; do not selectively omit poor outcomes. No benchmark figures in README or UI before a real run. CI gates: formatting, lint, types, unit and integration tests, frontend build, smoke E2E where feasible; secrets scan. Document skipped checks and exact blockers.

---

## 11 / Milestones and implementation gates

| Phase | Scope and exit condition |
|---|---|
| M0 Foundation | Repo, ADRs, locked deps, Compose, CI, .env.example, migrations, UI shell, /healthz and /readyz. One documented fresh-start command path; tests and UI build pass. |
| M1 Vertical slice | Payments REST + Support native MCP, seeded data, registry manual approval, four meta-tools, simple auth and actual discovery/invoke; UI Services/Registry/Playground/Executions show real state. Denied call test. |
| M2 MVP workflow | Communication MCP, finance/support grants, durable approvals/idempotency, duplicate workflow end-to-end, Overview/Audit/Approvals UI; notification sandbox and negative tests. Clear MVP boundaries. |
| M3 Crawler & protocols | OpenAPI + native MCP discovery, gRPC IT adapter, draft schema diff, version lifecycle and crawler UI. Validate registered endpoints. |
| M4 Discovery research | pgvector embeddings, FTS, hybrid fusion, labeled dataset, evaluation UI/API and comparison results; prefilter authorization. |
| M5 Domain breadth & governance | HR and Analytics, complete 36 tools, four agents, policy editor/simulator, quota controls, redaction/projection, all domain contracts. |
| M6 Reliability & operations | Full trace/metrics, reconcilers, circuit breakers where justified, CLI, responsive polish, operational docs; failure-injection and concurrency tests. |
| M7 Release & evaluation | Run benchmark suites, accessibility checks, complete documentation/demo and clean fresh install; publish measured results/limitations and tagged release. |

### Repository recommendation
```
apps/web/                  gateway/ (MCP, auth, execution, adapters)
control_plane/             services/{payments,support,it,hr,analytics,communication}/
agents/                    cli/                 shared/contracts/
db/migrations/             datasets/            benchmarks/       tests/
infra/                     docs/                .github/workflows/
compose.yaml                pyproject.toml       uv.lock           README.md
```

Each milestone: inspect repo → plan → implement small vertical slice → run actual tests → review changes → document decisions and failures → request approval to advance. No force pushes, destructive commands, secret copies or unsolicited cloud deployments. Commit only when requested or explicitly authorized. Capture commands, test counts and remaining issues. Keep ADRs for why technologies were chosen; do not overengineer with extra services before evidence.

---

## 12 / Definition of done and references

### Release checklist
- A fresh clone can start the local system with documented prerequisites and commands, run migrations and populate deterministic fixtures. Optional LLM and monitoring profiles have explicit setup.
- All advertised operations are implemented and covered by tests; disabled/deferred features are identified in the UI and README. Every visible page is consistent with the API and persisted state.
- A real MCP client can connect, list the four gateway tools and invoke an authorized business operation. REST and gRPC translations are validated against actual services.
- Primary duplicate-payment demo persists actual ticket/refund/notification records; denial, approval rejection, concurrent submission and timeout/reconciliation paths are demonstrable.
- Audit/trace evidence is redacted, searchable and linked to operations. Policy checks happen on the server and downstream scope enforcement is tested.
- Benchmarks are reproducible, with honest baseline comparisons, raw files, hardware/versions, known limits and no invented measurements.
- README includes architecture, bootstrap, screenshots from actual app, demo steps, data provenance, tests and limitations; docs include API contracts, threat model, ADRs, troubleshooting and changelog.

### Engineering reference material
These are architectural/implementation references to consult during development, not assertions that every proposed feature exists in another company's system. Verify current dependency documentation before locking versions.

- Uber Engineering: Designing MCP Gateway — https://www.uber.com/us/en/blog/designing-mcp-gateway/
- Model Context Protocol — https://modelcontextprotocol.io/
- FastMCP — https://gofastmcp.com/
- pgvector — https://github.com/pgvector/pgvector
- LangGraph — https://docs.langchain.com/oss/python/langgraph/overview
- OpenTelemetry — https://opentelemetry.io/

### Decisions and tradeoffs
Use simulated standalone services rather than real SaaS credentials; use Vite React UI and local Compose; start with a modular control/runtime boundary instead of forcing distributed deployment; use CPU-friendly retrieval; defer broad protocol support until the vertical slice is proven. These are recommended design choices, not externally mandated requirements. The SRS describes a planned system; no implementation, deployment or benchmark has been performed as part of preparing this document.
