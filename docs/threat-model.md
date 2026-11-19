# Threat model

The local gateway binds to loopback and the console proxies readiness and the authenticated control API on the same origin. PostgreSQL, Redis and the domain services stay on the Compose network. The sandbox assumes trust in the local Docker host and its DNS and network administration.

| Risk | Controls and remaining exposure |
|---|---|
| Credential disclosure | Database credentials come from environment variables. Configuration wraps connection URLs in secret types. Probe responses omit exception details. The local migration account is privileged, and anyone with Docker inspection rights can read container environment variables. |
| Compromised runtime | The API container uses a non-root UID and the runtime database role lacks domain schema privileges. Payments, Support, Communication, Inventory, IT Operations, HR, and Analytics each have a separate database login and internal service credential. Redis uses network isolation for this local milestone. |
| Dependency outage | Per-dependency deadlines, bounded connection pools and HTTP 503 allow callers to distinguish outages from process failure. Repeated checks avoid caching stale health responses. |
| Unauthorized tool access | Every REST and MCP runtime action validates the session and grant. Discovery filters published entries, unknown and denied IDs produce the same availability signal, and the runtime enforces the authorized customer scope. Direct API tests cover several denials. |
| Malicious input or service response | Request bodies and upstream responses are bounded. Validated Pydantic contracts permit only declared fields. The runtime rechecks the active version, enforces quotas, uses fixed Compose destinations and refuses redirects. The console renders service text as text. |
| Write uncertainty | Ticket creation is an ordinary sandbox write. It records a durable execution before dispatch and has no automatic retry. A transport failure can leave the ticket effect uncertain. Refunds bind the exact intent and use a distinct approver, a transactional ledger key, and ledger-only reconciliation. Communication stores a local simulated record and Support checks the linked refund and notification before resolution. |

The sandbox is intended for local use. External exposure requires a separate deployment design with authentication, TLS, managed credentials and a review of the network boundary.

### Crawler boundary

Crawler requests are limited to configured target IDs, protocols and ports. Name resolution is IPv4-only in the local sandbox and must yield RFC 1918 addresses. The crawler connects to a validated IP and sends the configured authority to the local service. HTTP clients refuse redirects and environment proxies. External OpenAPI references, unconfigured routes and unsupported inventory schema features cannot activate an adapter. The local Docker administrator controls DNS and networking. Arbitrary external registrations, TLS discovery and gRPC reflection are outside M3.

### Discovery and benchmark boundaries

The ranking query filters service grants, active versions, write permission and the current refund grant before either lexical or vector scoring. It returns scoped names and ranks. Query embeddings run in the API process using a pinned, checksum-verified local artifact. The API startup leaves model provisioning offline, and semantic requests fail visibly when the artifact or index is unavailable. Benchmark definitions reside in an isolated table and cannot be looked up or invoked through the runtime registry. Administrators control evaluation and export of its generated case data.

### Managed access and local domain boundaries

Profiles impose service ceilings. Explicit denies apply to discovery, schema access, invocation, and approval. The HR profile can access its assigned employee, that employee's manager relationship, and its department. Projected domain outputs expose only contract fields. The gateway masks common credential patterns in returned text. These rules do not provide general-purpose PII detection. Analytics receives read-only projections and cannot write to Payments tables. All communications remain local sandbox records.

## Operational evidence

Execution status events inherit the same caller, administrator, and approver scope as execution records. PostgreSQL captures events transactionally. The gateway database role can read those events and has no direct event insert, update, or delete privileges. Event rows contain status, safe error codes and timestamps. The gateway assigns request IDs itself. Prometheus labels omit principals, identifiers, arguments and query text. Keep metric scrapers and trace collectors on operator-controlled networks. Restrict the gateway ingress to public application routes if deploying beyond loopback.

## Provider evaluation boundary

The release harness restricts the gateway to loopback and uses disposable credentials with the same scoped catalog and execution APIs as ordinary agents. Ollama runs on loopback. The explicit OpenAI mode sends the benchmark input to the official HTTPS API. Provider input consists of authorized contract metadata and synthetic task descriptions. Fixture arguments and domain results stay within the evaluation client and gateway. The harness dispatches only the expected operation after verifying its published contract is read-only. Exports contain bounded selections, usage counters and execution metadata.
