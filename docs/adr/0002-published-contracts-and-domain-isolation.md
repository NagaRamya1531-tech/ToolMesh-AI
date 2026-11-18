# ADR 0002 — Published contracts and domain isolation

Accepted for M1.

Seed versioned, immutable contracts with the database migration. The administrator reviews a draft and publishes it with a reason. The tool's active-version pointer and publication record commit together. Keeping publication separate from discovery lets the crawler in M3 generate drafts without expanding the executable catalog.

Dispatch the M1 catalog through a fixed operation map and fixed Compose destinations. This gives the first REST and native MCP integrations a narrow egress surface. Dynamic endpoints will require explicit route validation, DNS and address checks, and drift handling.

Use separate logins and schemas for Payments and Support on one local Postgres server. Both services own their domain queries. The gateway has access to catalog, session and execution data. Distinct schemas reduce bootstrap cost and enforce a useful database boundary. They share a database server and associated capacity and failure modes.

Use expiring opaque sessions and code-defined grants for the local slice. Session lookup on each protected request gives revocation and keeps authorization independent from model arguments. Persist execution metadata before dispatch. Return validated, allowlisted data to the current caller and keep free-form payloads out of traces. M2 will add approval-bound refund execution and a reconciliation protocol.
