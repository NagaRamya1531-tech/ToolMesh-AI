# ADR 0008 · Provider evaluation outside the runtime

**Status** Accepted in M7

## Context

Release tests need reproducible workflow assertions and real evidence about model-driven tool selection. Model responses vary by version and provider, and sending domain data to a provider creates a separate trust boundary.

## Decision

Keep the deterministic LangGraph profiles as the application and CI workflow path. Evaluate model selection with an opt-in harness against a local Ollama endpoint and the same scoped catalog APIs used by agents. Supply only tool metadata and synthetic task text to the model. Pin the returned selection to a visible candidate, then invoke a verified read with fixed sample arguments. Use real provider usage counters in the evaluation export.

## Consequences

CI can run without an inference model or provider credentials. Provider comparisons share the live gateway authorization boundary while keeping the published experiment narrow. Model-generated arguments, model-driven approval flows and provider integration in the console remain future work.
