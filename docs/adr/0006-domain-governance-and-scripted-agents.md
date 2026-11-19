# ADR 0006 — Domain governance and scripted agent runs

Accepted for M5.

Keep service, operation, and customer scope checks in the gateway, with separate database roles and scoped queries at the domain boundary. A persisted agent identity selects a profile. The profile bounds access to configured services. An operation-specific policy can allow an ordinary write within that boundary or deny a tool. Explicit deny takes precedence, including during approval resumption. The HR profile is restricted to the assigned employee and department. Manager data is available only through the authorized employee's relationship.

Version policy changes and retain audit references. Store managed credentials as hashes and return the generated credential once at creation. Disabling an agent revokes its saved sessions. Store quota settings in PostgreSQL, then enforce the current limits against shared Redis counters. A settings change does not reset existing counters.

Use read-only Payments projections for Analytics. The Analytics role can read only the exported columns and write its own reports. Finance reports run in a repeatable-read transaction so sales and refunds share one snapshot. Analytics reports USD minor units for the sandbox.

Use LangGraph for bounded scripted profiles and persist the ordered execution references in PostgreSQL. Each step discovers eligible tools and invokes the regular runtime. Restart workflows pause for an independent approval, then resume with a health check. An interrupted ordinary write remains interrupted for review. The scripted agents use no model provider.
