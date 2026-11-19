# ADR 0009 · Read-only provider Playground

**Status** Accepted in M8

## Context

The Playground needs real model selection while preserving the gateway’s authorization and execution guarantees. A model’s choice and argument text are untrusted.

## Decision

Add a server-side, explicitly configured OpenAI route with a pinned model and one model request per task. Shortlist at most eight published read contracts from the caller's scoped catalog. Limit prompt size, serialized catalog size and output length. Apply the Redis gateway quota before a billable request. Revalidate the caller session, enforce the selected catalog version, and supply the caller’s customer scope. Run the selection through the existing invocation boundary with its read-only guard.

## Consequences

The provider sees synthetic task text and scoped schema metadata. Domain results stay within the gateway and console. Usage is returned with the request, and persisted execution evidence continues to record safe invocation metadata. The experiment excludes write orchestration and multi-step model planning.
