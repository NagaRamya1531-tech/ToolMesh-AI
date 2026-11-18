# ADR 0004 — Allowlisted crawler and gRPC translation

Accepted for M3.

Use a bounded PostgreSQL job worker. A claim records a lease, and an interrupted worker's job becomes eligible for another claim. One active job per service prevents overlapping claims during normal operation. A crawl stages published-source metadata within the same transaction as its result.

The configured target list is an execution boundary. The registry accepts target IDs, so administrators can enable services without supplying hosts, ports, operations or credentials. The crawler validates the resolved IP before connecting. Internal services use sandbox credentials and isolated schemas. A deployment that supports public or cross-network targets will need an explicit egress and TLS design.

Use checked-in generated Protobuf descriptors and stubs for the three IT operations. Reproduce them from the source file with the locked compiler.

```sh
uv run --locked python -m grpc_tools.protoc -I. --python_out=. --pyi_out=. --grpc_python_out=. shared/protos/it.proto
```

Preserve the validated contract for the curated adapters. Stage changed wire metadata for review and block incompatible curated contracts until their adapters are updated. The Inventory fixture demonstrates a bounded OpenAPI translation path, a versioned replacement and a live schema change. Its adapter accepts a small object schema subset and a fixed read route.
