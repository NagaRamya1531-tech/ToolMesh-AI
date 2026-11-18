# ADR 0005 — Authorization-prefiltered discovery

Accepted for M4.

Use PostgreSQL full-text search and pgvector with exact cosine distance over a materialized authorization-filtered relation. This gives deterministic results for the initial catalog and makes the prefilter easy to verify. Exact ranking scales linearly with the eligible catalog. Revisit approximate indexes and per-scope partitioning when measured load justifies the added cost of preserving authorization and rank quality.

Index immutable published versions by tool ID, version and model revision. A runtime query joins the active pointer, so disable, quarantine, rollback and grant revocation take effect without relying on cache invalidation. Semantic queries report missing model or indexing state explicitly. The pinned Potion model provides offline CPU inference. Its three files are downloaded explicitly and checked against SHA-256. [MinishLab model card](https://huggingface.co/minishlab/potion-base-8M/tree/bf8b056651a2c21b8d2565580b8569da283cab23)

Rank lexical and vector candidates independently, then combine rankings with reciprocal rank fusion. Use the same ranking SQL for runtime and the isolated evaluation corpus. Persist measured case-level evidence and remove transient benchmark documents. The full-schema control measures serialized bytes. Provider tokens, task success and end-to-end LLM latency require actual provider runs.
