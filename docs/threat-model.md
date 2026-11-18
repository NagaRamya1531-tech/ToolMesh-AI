# Threat model

The local gateway binds to loopback and the console proxies readiness and the authenticated control API on the same origin. PostgreSQL, Redis and the domain services stay on the Compose network. The sandbox assumes trust in the local Docker host and its DNS and network administration.

| Risk | Controls and remaining exposure |
|---|---|
| Credential disclosure | Database credentials come from environment variables. Configuration wraps connection URLs in secret types. Probe responses omit exception details. The local migration account is privileged, and anyone with Docker inspection rights can read container environment variables. |
| Compromised runtime | The API container uses a non-root UID and the runtime database role lacks domain schema privileges. Payments, Support and Communication each have a separate database login and internal service credential. Redis uses network isolation for this local milestone. |
| Dependency outage | Per-dependency deadlines, bounded connection pools and HTTP 503 allow callers to distinguish outages from process failure. Repeated checks avoid caching stale health responses. |
| Unauthorized tool access | Every REST and MCP runtime action validates the session and grant. Discovery filters published entries, unknown and denied IDs produce the same availability signal, and the runtime enforces the authorized customer scope. Direct API tests cover several denials. |
| Malicious input or service response | Request bodies and upstream responses are bounded. Validated Pydantic contracts permit only declared fields. The runtime rechecks the active version, enforces quotas, uses fixed Compose destinations and refuses redirects. The console renders service text as text. |
| Write uncertainty | Ticket creation is an ordinary sandbox write. It records a durable execution before dispatch and has no automatic retry. A transport failure can leave the ticket effect uncertain. Refunds bind the exact intent and use a distinct approver, a transactional ledger key, and ledger-only reconciliation. Communication stores a local simulated record and Support checks the linked refund and notification before resolution. |

The sandbox is intended for local use. External exposure requires a separate deployment design with authentication, TLS, managed credentials and a review of the network boundary.

### Crawler boundary

Crawler requests are limited to configured target IDs, protocols and ports. Name resolution is IPv4-only in the local sandbox and must yield RFC 1918 addresses. The crawler connects to a validated IP and sends the configured authority to the local service. HTTP clients refuse redirects and environment proxies. External OpenAPI references, unconfigured routes and unsupported inventory schema features cannot activate an adapter. The local Docker administrator controls DNS and networking. Arbitrary external registrations, TLS discovery and gRPC reflection are outside M3.
