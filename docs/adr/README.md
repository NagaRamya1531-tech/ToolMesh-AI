# Architecture decision records

| Record | Decision |
|---|---|
| [0001 · Local service boundaries](0001-local-service-boundaries.md) | Run a Compose sandbox with explicit module and domain boundaries. |
| [0002 · Published contracts and domain isolation](0002-published-contracts-and-domain-isolation.md) | Dispatch against reviewed versions and give each domain an isolated database role. |
| [0003 · Refund approval and reconciliation](0003-refund-approval-and-reconciliation.md) | Bind exact refund intent, consume approval atomically, and read the ledger to resolve uncertainty. |
| [0004 · Allowlisted crawler and gRPC](0004-allowlisted-crawler-and-grpc.md) | Constrain discovery to configured targets and use checked-in Protobuf descriptors. |
| [0005 · Authorization-prefiltered discovery](0005-authorization-prefiltered-discovery.md) | Apply access policy before lexical or vector scoring and isolate the benchmark corpus. |
| [0006 · Domain governance and scripted agents](0006-domain-governance-and-scripted-agents.md) | Apply versioned policy across the six service domains and persist bounded agent execution steps. |
| [0007 · Bounded retries and execution evidence](0007-bounded-retries-and-execution-evidence.md) | Retry eligible reads within a deadline, preserve uncertain writes, and record transactional execution timelines. |
