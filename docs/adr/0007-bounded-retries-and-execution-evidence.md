# ADR 0007 · Bounded retries and execution evidence

**Status** Accepted in M6

## Context

A transport timeout can mean a service never received a call or committed it before the response was lost. Operators also need evidence after a gateway process restarts.

## Decision

Use up to three attempts within seven seconds for ordinary reads that encounter recognized transient faults. Apply exponential jitter between attempts. Count exhausted invocations per service in a process-local circuit. After three such failures, wait 15 seconds before allowing one probe. Use one dispatch for writes. Route uncertain approval-gated writes through the existing domain receipt reconciliation.

Record execution status transitions in the same PostgreSQL transaction as the corresponding state change. Store server-generated request and OpenTelemetry trace IDs with execution metadata. Use an application-owned Prometheus registry with fixed route, service and outcome labels. Keep optional OTLP export asynchronous. Classify stale ordinary rows in bounded, lock-skipping batches and preserve uncertain outcomes for review.

## Consequences

Timeline evidence survives process restarts and honors existing execution visibility. Circuit state resets on restart and exists separately on each process. Pre-M6 executions have no historical timeline. An ordinary write may remain unknown until domain evidence is reviewed. Process-local metric counters reset with the gateway, so monitoring should use a scraper for historical trends.
