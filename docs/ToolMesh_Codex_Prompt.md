# Codex implementation prompt — ToolMesh AI

Act as a senior platform engineer and frontend product engineer. Build ToolMesh AI, a production-style enterprise MCP gateway and agent infrastructure platform. Read `docs/ToolMesh_AI_SRS.md` in full before changing anything. It is the authoritative product specification. This is an independent project inspired by public MCP gateway design, not an Uber clone or a toy chatbot.

## First session: inspect, plan, then implement M0 only
1. Inspect the working directory, existing files, git status, dependency manifests, README and any AGENTS.md. Do not overwrite existing work. If the SRS is missing, stop and tell me where to place it; do not improvise a replacement.
2. Summarize the architecture, constraints, key risks and your M0 plan with concrete files, commands, test strategy and acceptance checks. Surface any specification conflicts; prefer a small reversible choice and explain it.
3. Implement M0 only unless I explicitly authorize a later milestone. Build the real foundation: Python dependency lock, FastAPI app/control and runtime module boundaries, frontend React/TypeScript shell, Compose with PostgreSQL/pgvector and Redis, migrations, environment template, health/readiness checks, baseline test suite and CI. Verify library compatibility before pinning. Do not implement the entire product in one pass.
4. Run the checks you can run in this environment. Include command output summaries, test counts, failures and blockers. If Docker or a dependency is unavailable, distinguish what was validated from what was not. Never claim tests passed without executing them.
5. Stop at the M0 gate. Show changed files, key design tradeoffs, how to run locally, screenshots or UI verification if available, and what M1 will implement. Do not automatically proceed to M1, push, deploy or purchase anything.

## Product expectations for every milestone
- Build a genuine MCP gateway (compatible MCP client/server and four meta-tools), not a REST-only facade. Separate control-plane management from runtime policy enforcement. Use actual domain services, persisted records and realistic validations, not constant JSON responses.
- MVP: payments REST, support native MCP, communication MCP; primary duplicate-payment scenario with a real ticket, duplicate evidence, separate finance request, authorized human approval, idempotent sandbox refund, local notification and trace. Keep non-MVP modules clearly marked unavailable until implemented.
- Full scope: six services, 36 functional business tools, 4 meta-tools, four agent profiles, REST/MCP/gRPC adapters, crawler, registry versioning, hybrid retrieval, governance, observability, CLI and evaluations. The 10k synthetic catalog is benchmark data, not 10k functional tools.
- Security: deny by default, verify identity, authorize every discovery/schema/invocation, enforce object scope, validate schema, redact sensitive fields, allowlisted outbound hosts, rate limits, durable approval binding, idempotency and unknown-outcome reconciliation. Never trust LLM-generated identity or tool content. No live payments, system restarts or email delivery by default. No secrets in git, logs or UI.
- Implementation quality: typed boundaries, clean modules, migrations, stable errors, explicit deadlines, structured logs, real integration tests, deterministic seed fixtures, reproducible setup and scoped changes. Avoid unnecessary abstractions or premature microservices. Keep docs/ADRs and changelog consistent with actual behavior.

## Frontend is a primary deliverable
Implement a professional infrastructure console, not a marketing site, demo cards or generic chatbot. Use the SRS route-by-route requirements. Design a consistent 240px desktop sidebar, compact top bar, breadcrumbs, workspace/environment label, data-dense tables, carefully styled charts and contextual details. Use a restrained dark slate/indigo palette, readable typography, subtle borders, consistent 4/8px spacing, accessible contrast and clear status labels. Mobile drawer and responsive table layouts are required. No gradient-heavy hero, decorative charts or buttons that do nothing.

Build reusable AppShell, PageHeader, DataTable, FilterBar, StatusBadge, EmptyState, ErrorState, DetailDrawer, ConfirmDialog, CodeViewer and TraceTimeline as needed; use accessible components rather than reinventing controls. Use typed API client, TanStack Query and real data; no hardcoded dashboards, random metrics or fake successful operations. Route filters are shareable; handle loading, error, forbidden, empty, stale and pending states. Disable duplicate submits; display server validation errors and persisted mutation outcomes. Use seeded data only and label sandbox mode. The Overview must drill down into actual records. Registry must show schema/version diff. Playground must show ordered calls and safe data, not internal model reasoning. Approval screen must show bound intent and update actual state. Test keyboard, responsive 1440/1024/390px and Playwright workflows. If no browser tooling is available, report that visual verification is pending rather than inventing it.

## Workflow and evaluation rules
Use seeded, deterministic scripted agent runs for reliable CI, and separately test actual LLM provider mode via environment variables. Label both honestly. Keep token, latency and success metrics from recorded real runs only. Benchmark search and runtime separately; compare keyword/vector/hybrid and feasible full-tool baseline; include total tokens and LLM overhead, errors and limitations. No benchmark claims without raw results.

## Working agreement
- Work milestone by milestone; before each new milestone provide a plan and wait for my authorization to advance. Keep code changes scoped and understandable. Explain important code and design choices so I can review them myself.
- Use `git diff --stat` and relevant diffs to review work. Do not rewrite history, delete unrelated files, commit, push, deploy or enable external paid services without my permission. Do not silently edit the SRS; propose changes separately.
- Create/update README, architecture notes, ADRs and changelog as part of each milestone. Include exact setup/test commands and explicit limitations. Test failures are work items, not something to hide.
- For each milestone output: what changed, why, files, commands, test evidence, frontend verification, security considerations, known gaps and next acceptance gate.

Begin by reading the SRS and inspecting the repository. Then start M0 as described above.
