# Architecture

Compose starts a local Postgres instance with pgvector, Redis, the Alembic migration job, FastAPI and an Nginx-hosted React console. The browser sends readiness queries to the same origin. Nginx forwards them to FastAPI over the Compose network. Vite provides the equivalent proxy during frontend development.

The gateway exposes a FastAPI control API and a FastMCP Streamable HTTP endpoint. The control plane owns publication and catalog metadata. Invocation through either entry point uses the same gateway runtime. The registry contains immutable JSON Schema snapshots and an active-version pointer. A transaction locks the tool row, records the approval reason and actor, and sets the active version.

Payments runs as a REST service and Support runs as a native MCP server. Each accesses its own PostgreSQL schema through a distinct login. The gateway dispatches to fixed Compose destinations with separate internal credentials. It validates the persisted contract against the running adapter, verifies the authenticated caller's customer scope and sends the authorized customer ID downstream. Both services query by that customer ID.

Local principals have explicit service and write grants defined in code. The database stores only hashed opaque session tokens and their expiry. Runtime does not cache allow decisions. Redis enforces fixed windows for login and invocation. Before dispatch, PostgreSQL stores a trace ID, caller, tool version and running status. Successful outcomes require contract-validated downstream data. Failed writes whose effect is uncertain return `UNKNOWN`. Traces persist metadata, rather than caller payloads or free-form service results.

## Control plane

The administrator and crawler own catalog changes. The indexer derives searchable metadata from published versions. Evaluation jobs use a separate corpus.

```mermaid
flowchart LR
    Admin["Platform administrator"] --> API["Control API"]
    API --> Jobs[("PostgreSQL crawl jobs")]
    API --> Catalog[("PostgreSQL catalog and release history")]
    API --> Eval[("PostgreSQL evaluation runs")]
    Jobs --> Crawl["Crawler worker"]
    Crawl --> Target["Configured service metadata"]
    Crawl --> Catalog
    Catalog --> Index["Published-version indexer"]
    Index --> Discovery[("PostgreSQL search terms and pgvector")]
    Eval --> Compare["Evaluation worker"]
    Compare --> Scratch[("Isolated benchmark corpus and case results")]
```

## Data plane

The browser and native MCP client share the same runtime policies. Each domain service uses an isolated PostgreSQL role.

```mermaid
flowchart LR
    Caller["MCP client or Playground"] --> Auth["Session and customer scope"]
    Auth --> Runtime["Gateway runtime"]
    Runtime --> Catalog[("PostgreSQL active versions and executions")]
    Runtime --> Quota[("Redis quotas")]
    Runtime --> REST["Payments and Inventory REST"]
    Runtime --> MCP["Support and Communication MCP"]
    Runtime --> GRPC["IT Operations gRPC"]
    REST --> State[("Isolated domain schemas")]
    MCP --> State
    GRPC --> State
```

### Agent discovery

```mermaid
sequenceDiagram
    participant A as MCP client
    participant G as Gateway runtime
    participant P as PostgreSQL
    participant E as Local embedding model
    A->>G: discover_server
    G->>P: Check session and eligible services
    G-->>A: Eligible servers
    A->>G: discover_tools with query and strategy
    G->>P: Authenticate the caller
    opt Vector or hybrid query
        G->>E: Encode the query locally
    end
    G->>P: Filter current grants and active versions, then rank
    G-->>A: Eligible tool IDs and ranks
    A->>G: get_tool_schema
    G->>P: Recheck caller eligibility and active version
    G-->>A: Published schema or unavailable
```

### Tool execution

```mermaid
sequenceDiagram
    participant A as MCP client or Playground
    participant G as Gateway runtime
    participant P as PostgreSQL
    participant R as Redis
    participant D as Domain service
    A->>G: Invoke tool with version and arguments
    G->>P: Authenticate, create execution, check active version and grants
    G->>G: Validate contract and customer scope
    G->>R: Enforce principal and tool quotas
    alt Allowed ordinary operation
        G->>P: Recheck active version and persist RUNNING
        G->>D: Dispatch scoped call
        D-->>G: Result or normalized error
        G->>P: Record execution outcome
        G-->>A: Validated result or error and trace ID
    else Allowed approval-gated operation
        G->>P: Persist bound approval and PENDING_APPROVAL
        G-->>A: Approval and trace ID
    else Denied invocation
        G->>P: Record denial for authenticated caller
        G-->>A: Denial and trace ID
    end
```

## Health contract

| Endpoint | Behavior |
|---|---|
| `GET /healthz` | HTTP 200 and process liveness. Works during dependency failure. |
| `GET /readyz` | HTTP 200 when Postgres, pgvector, the expected migration revision, the sandbox workspace and Redis are usable. HTTP 503 with the same response schema when a check fails. |

Probes run concurrently under individual deadlines. Responses include a server-generated request ID, bypass caching and return bounded status values with UTC check time. The expected revision in the readiness query must be updated when a migration advances the schema. The console refreshes every 30 seconds and supports a manual retry. When a request fails after a valid response, the console labels the retained result as the last successful check.

[ADR 0001](adr/0001-local-service-boundaries.md) · [Threat model](threat-model.md)

[ADR 0002](adr/0002-published-contracts-and-domain-isolation.md)

## Guided workflow and refund approval

A durable workflow record links the duplicate-check trace, structured Support ticket evidence, refund approval and simulated notification. Support starts and closes the investigation. Finance selects an evidenced capture and requests the refund through the runtime. A separate approver reviews the canonical intent. The scripted workflow carries IDs across handoffs and never takes another principal's identity.

The control plane serializes each approval decision with a PostgreSQL row lock. Approval consumption, the audit event and the execution transition to running commit together. The gateway rechecks the requester grant, customer scope, published version, matching contract and canonical intent before consumption. Payments uses the approval UUID as its ledger key and locks the capture before checking currency and total refunded balance. Reconciliation reads the ledger by that UUID, validates the complete receipt and updates the execution only after confirmation.

Communication runs as a third isolated MCP service. Its local message outbox enforces one simulated notification per customer and refund. Support changes the ticket status only after the gateway confirms that the refund belongs to the ticket's order and the outbox contains the corresponding notification.

[ADR 0003](adr/0003-refund-approval-and-reconciliation.md)

## Discovery and release lifecycle

The service registry maps stable IDs to operator-configured Compose targets. Administrators register targets and can edit their display names. The read-only probes fetch authenticated OpenAPI descriptions and native MCP tool lists. For IT, the crawler reads the generated Protobuf descriptor after confirming the configured endpoint over gRPC. It skips operations outside the adapter map and exposes unsupported entries in the crawl result.

A PostgreSQL job queue claims work with row locks and a finite lease. Each successful job commits its output and draft changes together. Immutable schema versions contain their source metadata. Added, removed, changed and required fields are shown in the review UI. A breaking or missing source revokes the active pointer and records quarantine in release history. Restoring discovery requires manual approval of a compatible replacement. Curated adapters keep their static serializers and block incompatible wire contracts until the adapter has been reviewed.

[ADR 0004](adr/0004-allowlisted-crawler-and-grpc.md)

## Ranked discovery

Runtime tool search materializes the authorization-filtered active catalog before scoring. PostgreSQL builds weighted English full-text terms from each version's safe name and description. It also stores a 256-dimensional pgvector row for every active version indexed with the pinned Model2Vec model. Query embeddings are produced locally. Keyword and exact cosine rankings use stable tie breaks. Hybrid search applies reciprocal rank fusion with a rank constant of 60 and up to 100 candidates from each strategy.

Evaluation jobs use a separate document table keyed by run. Their label fixtures simulate scope restrictions and never enter the executable catalog. Progress and case results survive process restarts. The worker regenerates an interrupted run from its seed and clears transient copies of benchmark documents after completion. Exports retain the configuration, fingerprint, case labels, ranked IDs, and raw measurements.

[ADR 0005](adr/0005-authorization-prefiltered-discovery.md)

## Refund approval

```mermaid
sequenceDiagram
    participant F as Finance requester
    participant G as Gateway runtime
    participant P as PostgreSQL
    participant A as Refund approver
    participant D as Payments
    F->>G: Request a scoped refund with an idempotency key
    G->>P: Bind transaction, amount, currency, scope, and version
    G-->>F: PENDING_APPROVAL with trace ID
    A->>G: Submit a decision with a reason
    G->>P: Lock approval and recheck grant, version, intent, and expiry
    alt Approval consumed
        G->>P: Commit approval, audit event, and RUNNING
        G->>D: Dispatch using approval ID as ledger key
        G-->>A: Confirmed, failed, or unknown execution
    else Rejected or unavailable
        G-->>A: Decision or conflict response
    end
```

The requester and approver are separate sandbox identities. Approver actions and protected outcomes are recorded for audit. [Threat model](threat-model.md) · [Refund ADR](adr/0003-refund-approval-and-reconciliation.md)

## Tool registration and version lifecycle

```mermaid
flowchart LR
    Target["Configured target"] --> Register["Register service"]
    Register --> Crawl["Queue crawl"]
    Crawl --> Draft["Stage immutable draft"]
    Draft --> Review["Review schema and diff"]
    Review --> Publish["Publish active version"]
    Publish --> Drift{"Breaking or missing source"}
    Drift -->|Yes| Quarantine["Quarantine active version"]
    Drift -->|No| Publish
    Quarantine --> Draft
    Publish --> Disable["Disable or archive"]
    Publish --> Rollback["Roll back to a compatible release"]
    Rollback --> Publish
```

Publication is an explicit administrator decision. A disabled tool can be republished; an archived tool cannot. Compatible rollback is limited to earlier published versions that satisfy current source rules.

## Database relationships

These ER diagrams show selected entities from the Alembic migrations. A dashed line represents a non-identifying foreign-key relationship in Mermaid notation. Cross-service correlations between the execution ID, ledger ID, ticket ID, and notification ID are maintained by the application and omitted from these foreign-key views.

### Registry and retrieval

```mermaid
erDiagram
    SERVICES ||..o{ TOOLS : contains
    SERVICES ||..o{ CRAWL_JOBS : queues
    TOOLS ||--o{ TOOL_VERSIONS : versions
    TOOLS ||..o{ PUBLICATIONS : records
    TOOL_VERSIONS ||--o{ TOOL_EMBEDDINGS : indexes
    EVALUATION_RUNS ||--o{ EVALUATION_CASES : records
    EVALUATION_RUNS ||--o{ EVALUATION_DOCUMENTS : stages
    TOOLS {
        text id PK
        text service_id FK
        integer active_version FK
        text lifecycle
    }
    TOOL_VERSIONS {
        text tool_id PK, FK
        integer version PK
        jsonb contract
        tsvector search_terms
    }
    TOOL_EMBEDDINGS {
        text tool_id PK, FK
        integer version PK, FK
        text model PK
        vector embedding
    }
```

The active pointer is a nullable composite reference from the tool ID and active version into `tool_versions`. Benchmark documents are transient. Run and case records remain available for export.

### Execution and domain state

```mermaid
erDiagram
    EXECUTIONS ||--o| APPROVALS : binds
    APPROVALS o|..o{ DUPLICATE_WORKFLOWS : follows
    PAYMENT_TRANSACTIONS ||..o{ PAYMENT_REFUNDS : funds
    IT_SERVICES ||..o{ IT_INCIDENTS : reports
    EXECUTIONS {
        uuid id PK
        text principal
        text status
    }
    APPROVALS {
        uuid id PK, FK
        text requester
        text status
        text intent_hash
    }
    DUPLICATE_WORKFLOWS {
        uuid id PK
        uuid refund_id FK
        uuid ticket_id
        uuid notification_id
    }
    PAYMENT_REFUNDS {
        uuid id PK
        text transaction_id FK
        integer amount_minor
    }
```

## Failure recovery

```mermaid
flowchart LR
    Write["Approved refund dispatch"] --> Result{"Response received"}
    Result -->|Confirmed receipt| Success["Record SUCCEEDED"]
    Result -->|Ambiguous outcome| Unknown["Record UNKNOWN"]
    Unknown --> Check["Reconcile by reading the ledger"]
    Check --> Match{"Exact receipt found"}
    Match -->|Yes| Success
    Match -->|No| Unknown
    Result -->|Definitive failure| Failed["Record FAILED"]
```

The reconciler does not replay writes. Expired or rejected approvals and revoked grants block dispatch. Quota unavailability blocks writes. Interrupted crawl jobs can be reclaimed after their lease expires. Interrupted evaluation runs rebuild deterministic inputs from their stored configuration. [Operational procedures](runbook.md#diagnose-startup-and-dependency-failures)

## Local deployment

```mermaid
flowchart LR
    Browser["Browser on loopback"] -->|Published port 5173| Web
    Client["MCP client on loopback"] -->|Published port 8000| Gateway
    subgraph Compose["Compose network"]
        Web["Nginx console"]
        Gateway["Gateway"]
        Web --> Gateway
        Store[("PostgreSQL and pgvector")]
        Migration["One-shot Alembic migration"]
        Cache[("Redis")]
        Backend["Domain service containers"]
        Store --> Migration
        Migration -->|Successful exit| Gateway
        Migration -->|Successful exit| Backend
        Cache --> Gateway
        Gateway --> Backend
        Backend --> Store
    end
```

The gateway joins the Compose network and publishes its HTTP port on loopback. PostgreSQL, Redis, and domain services have no host port bindings. The optional embedding files mount read-only. The [runbook](runbook.md) covers bootstrap and faults.

## Technology choices

| Technology | Engineering rationale |
|---|---|
| FastAPI and FastMCP | Typed HTTP contracts and native MCP Streamable HTTP share one invocation path. |
| PostgreSQL and Alembic | Transactions coordinate catalog, decision, and execution state. Migrations declare domain roles and schema evolution. |
| PostgreSQL text search and pgvector | Weighted lexical rank and exact cosine ranking use the same scoped relational dataset. |
| Redis | Shared quota counters keep rate-limit state separate from the transactional ledger. |
| Protobuf and gRPC | Checked-in descriptors define the IT contract and support reproducible stub generation. |
| React, TypeScript, Vite, and Nginx | A typed console builds to static assets. Nginx serves it and forwards same-origin API requests. |
| Model2Vec | The pinned CPU model provides local semantic retrieval without an inference service dependency. |

See the [ADR index](adr/README.md) for the tradeoffs and accepted boundaries.
