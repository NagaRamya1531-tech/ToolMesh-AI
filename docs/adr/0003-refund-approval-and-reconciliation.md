# ADR 0003 — Refund approval and reconciliation

Accepted for M2.

Use the same UUID for the approval, execution and downstream refund ledger entry. The runtime binds the requester, tool, version and canonical arguments to a hash. The request idempotency key is scoped to the requester and tool. Reusing it with changed intent returns a conflict. Replay of the same intent returns the existing approval and execution status.

Consume the pending approval in PostgreSQL before attempting the downstream effect. This makes concurrent decisions resolve to one winner and gives the worker durable correlation after a crash. The gateway rechecks the grant, scope, active catalog version and matching contract as part of consumption. Payments serializes writes for the capture and commits the refund and ledger evidence in one transaction. Only the Finance sandbox identity can request refunds and only the separate approver identity can decide.

A timeout may occur after the ledger commits. Report `UNKNOWN`, then reconcile with a bounded, read-only background pass. A matching receipt moves the execution to `SUCCEEDED`. If a process exits before dispatch or the receipt cannot be proven, execution remains `UNKNOWN`. Automated write replay and a manual reconciliation console are future reliability work.

Store a separate workflow record so Support, Finance and the approver can sign in independently. The M2 script is an explicit persisted state machine with bounded service calls. LangGraph-based model execution and additional agent scenarios are a separate extension.
