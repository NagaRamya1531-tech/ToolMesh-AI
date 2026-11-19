# M9.1.1 · Protected execution contracts and schema

**Baseline:** `3e4e5b900ecbad0a76184d3040661ddb2e9ef73f`
**Migration:** `0009 → 0010`
**Scope:** Synthetic sandbox data for `payments.issue_refund` and `it.restart_service`.

M9.1.1 adds persistence contracts and an additive schema for Slice A. The new dispatcher is dormant. The gate is seeded in LEGACY mode so existing approval flows remain available. Only migrator-owned schema objects and synthetic coordination records are added. The gateway app role receives no privileges on the new tables in this milestone.

## Contracts and schema

| Object | Invariant |
|---|---|
| `execution_commands` | One row per existing execution and its matching approval scope, with a foreign key to the pinned tool version. The same UUID is the domain operation ID. Work states, lease pairing, generation, deadlines, payload shape and verification-only eligibility have database checks. |
| `execution_request_keys`, `execution_legacy_key_aliases` | A scoped key maps to one canonical execution. A legacy alias preserves the original requester/tool uniqueness namespace and references the canonical reservation. Digest key versions do not create a separate uniqueness namespace. |
| `execution_attempts` | Unique execution/generation, with explicit SEND and VERIFY shapes. Verification cannot record a send marker. A partial unique index limits each command to one marked send, consistent with replay being disabled in Slice A. |
| `execution_receipts` | One canonical receipt per execution. Its definitive outcome must match the referenced completed attempt. A receipt can retain evidence metadata after removal of its optional payload. |
| `execution_approval_entitlements` | Principal, customer and tool form the key. The existing sandbox approver is seeded for the two protected tools. Runtime authorization remains on the existing path. |
| `execution_authorization_guard`, `execution_dispatch_gate` | Singleton rows with constrained values. The revision guard starts at 1 and the gate starts at LEGACY. |
| Partial queue indexes | Scheduled READY work, expiring leases and due UNKNOWN verification each have a time/ID index. |

The Pydantic models distinguish approval state from execution state. READY, CLAIMED and DISPATCHING project to RUNNING. UNKNOWN remains distinct and the Slice A transition graph has no UNKNOWN-to-READY edge. PostgreSQL enforces row shapes, identity and references. Conditioned transitions, projection writes and lock ordering will belong to the authorized store and worker milestones. The payload envelope validates storage shape. It performs no encryption or authentication. [Contracts](../../gateway/execution_contracts.py) · [Migration](../../db/migrations/versions/0010_durable_execution_schema.py)

## Upgrade, downgrade and compatibility

A blank pgvector sandbox can migrate to head. Upgrading a seeded 0009 sandbox leaves the original executions, approvals, status events and audit rows intact. Revision 0010 adds a uniqueness constraint to approvals to enforce the new foreign-key binding. It does not backfill commands, keys or receipts. The existing status trigger still records changes. Downgrade is permitted on an empty ledger with the gate in LEGACY mode, and is refused if commands or reservations exist or the gate has left LEGACY.

The first Compose boot exposed an exact-0009 readiness check. The query now accepts both 0009 and 0010, including safe downgrade. Startup, recovery and adapter behavior are otherwise untouched. [Readiness query](../../gateway/app.py#L125)

## Verification

The initial implementation run passed 46 focused tests (four contract and 42 database tests), 29 Python Compose tests (25 existing and four contract tests), and 14 Chromium tests. The Python Compose run had no skips. Ruff lint and formatting, mypy, and the frontend production build passed. Vite reported dependency module-directive warnings while bundling. No live provider calls were made. Both regression stacks reported revision 0010, gate LEGACY, and zero commands after the suites.

The migration tests provision separate PostgreSQL containers for a fresh installation and an upgrade from 0009 with seeded history. The upgrade compares two execution rows, two approval rows, four execution status events and two audit rows before and after migration. SQL checks cover uniqueness, key alias binding, state validity, verification restrictions, receipt outcome matching, foreign keys and guarded downgrade. [Migration tests](../../tests/test_durable_execution_schema.py) · [Contract tests](../../tests/test_execution_contracts.py)

Run the focused suite using the repository PostgreSQL image built from [infra/postgres.Dockerfile](../../infra/postgres.Dockerfile). The verification run used the existing local build tagged `toolmesh-auditpy-postgres`.

```sh
TOOLMESH_TEST_SCHEMA_IMAGE=toolmesh-auditpy-postgres uv run --locked pytest -q tests/test_execution_contracts.py tests/test_durable_execution_schema.py
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy gateway control_plane shared services benchmarks
npm --prefix apps/web run build
```

The isolated Compose projects used `.env.example`, loopback ports and the pinned local discovery model. OpenAI and remote trace export were disabled.

```sh
API_PORT=18141 WEB_PORT=15141 TOOLMESH_OPENAI_API_KEY= TOOLMESH_OTLP_TRACES_ENDPOINT= docker compose --env-file .env.example -p toolmesh-m911-reg up --build -d --wait
API_PORT=18142 WEB_PORT=15142 TOOLMESH_OPENAI_API_KEY= TOOLMESH_OTLP_TRACES_ENDPOINT= docker compose --env-file .env.example -p toolmesh-m911-browser up --build -d --wait
TOOLMESH_TEST_API=http://127.0.0.1:18141 TOOLMESH_TEST_REQUIRE_MODEL=1 TOOLMESH_TEST_MUTATE_POLICY=1 TOOLMESH_TEST_COMPOSE_PROJECT=toolmesh-m911-reg uv run --locked pytest -q -rs --ignore=tests/test_durable_execution_schema.py
export TOOLMESH_TEST_WEB=http://127.0.0.1:15142
TOOLMESH_EXPECT_DRAFT=1 npm --prefix apps/web run test:e2e -- overview.spec.ts workspace.spec.ts provider.spec.ts sidebar.spec.ts
npm --prefix apps/web run test:e2e -- refund.spec.ts crawler.spec.ts
npm --prefix apps/web run test:e2e -- evaluations.spec.ts
npm --prefix apps/web run test:e2e -- access.spec.ts
git diff --check
```

Release gates include key custody, retention, live ownership transfer and adapter conformance under overlapping requests. Migration tests verify schema behavior in disposable instances. They do not establish runtime fencing, encrypted payload safety or downstream replay guarantees.

## Independent review hardening

The review adds tests only. Migration 0010, the execution contracts, and runtime behavior are unchanged.

- Checked effective access as `toolmesh_app` using PostgreSQL privilege predicates, which include inherited and PUBLIC grants. All eight new tables deny SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES and TRIGGER. Also checked SELECT, INSERT, UPDATE and REFERENCES at column level. The role retained SELECT on legacy execution events.
- Exercised accepted command and key bindings for both protected operations. Checked unsupported tools, a missing catalog version even when legacy approval data matches it, and cross-tool use of an approval.
- Checked duplicate and false singleton keys, privileged deletion, incomplete command and receipt envelopes, invalid nonces and key versions, unpaired lease fields, zero-generation ownership, leases on terminal states, and a verification-only sendable state. Retained the existing tests for receipts before attempt completion and receipt outcome mismatches.
- Verified successful downgrade removes all eight tables and returns the database to 0009. Verified downgrade refusal for a populated ledger and for an empty ledger with a PAUSED or missing gate. Each refusal preserved revision 0010, all ledger tables, and legacy history. Gate mutation was confined to disposable test databases and restored to LEGACY.

The singleton constraints enforce at most one row. A privileged database owner can remove the seeded guard or gate. Missing gate blocks downgrade. Future runtime admission must also fail closed when either coordination row is absent. The envelope checks establish storage shape only. [Database tests](../../tests/test_durable_execution_schema.py) · [Migration](../../db/migrations/versions/0010_durable_execution_schema.py)

### Review verification commands

The focused run passed 80 cases, including four contract cases and 38 PostgreSQL cases on each of the fresh and upgraded databases. The full Python run passed 105 tests with zero skips. Ruff lint and formatting, mypy, and the production build passed. Vite emitted the existing dependency module-directive warnings.

The first Chromium run had 13 passes and one failure. The policy simulation endpoint returned HTTP 500 and the expected status did not render. All 14 cases passed on a fresh disposable stack. The initial failure did not reproduce, and its cause remains unconfirmed. Both review Compose databases reported revision 0010, gate LEGACY, and zero durable commands. Durable dispatch remains disabled.

```sh
TOOLMESH_TEST_SCHEMA_IMAGE=toolmesh-auditpy-postgres uv run --locked pytest -q tests/test_execution_contracts.py tests/test_durable_execution_schema.py
API_PORT=18161 WEB_PORT=15161 TOOLMESH_OPENAI_API_KEY= TOOLMESH_OTLP_TRACES_ENDPOINT= docker compose --env-file .env.example -p toolmesh-m911-hardening-py up --build -d --wait
API_PORT=18162 WEB_PORT=15162 TOOLMESH_OPENAI_API_KEY= TOOLMESH_OTLP_TRACES_ENDPOINT= docker compose --env-file .env.example -p toolmesh-m911-hardening-web up --build -d --wait
TOOLMESH_TEST_SCHEMA_IMAGE=toolmesh-auditpy-postgres TOOLMESH_TEST_API=http://127.0.0.1:18161 TOOLMESH_TEST_REQUIRE_MODEL=1 TOOLMESH_TEST_MUTATE_POLICY=1 TOOLMESH_TEST_COMPOSE_PROJECT=toolmesh-m911-hardening-py uv run --locked pytest -q -rs
export TOOLMESH_TEST_WEB=http://127.0.0.1:15162
TOOLMESH_EXPECT_DRAFT=1 npm --prefix apps/web run test:e2e -- overview.spec.ts workspace.spec.ts provider.spec.ts sidebar.spec.ts
npm --prefix apps/web run test:e2e -- refund.spec.ts crawler.spec.ts
npm --prefix apps/web run test:e2e -- evaluations.spec.ts
npm --prefix apps/web run test:e2e -- access.spec.ts
# Before the browser rerun, remove only the disposable browser sandbox and repeat its setup and test commands.
API_PORT=18162 WEB_PORT=15162 TOOLMESH_OPENAI_API_KEY= TOOLMESH_OTLP_TRACES_ENDPOINT= docker compose --env-file .env.example -p toolmesh-m911-hardening-web down --volumes
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy gateway control_plane shared services benchmarks
npm --prefix apps/web run build
git diff --check
```
