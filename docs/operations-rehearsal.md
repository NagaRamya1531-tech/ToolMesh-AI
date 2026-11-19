# M6 operations rehearsal

## Method

The client uses a published version 1 contract for `payments.get_transaction`, a synthetic transaction, and a scoped Payments identity. It warms the path with one invocation, then submits five consecutive batches of four concurrent requests. Each sample measures client wall time from POST dispatch to receipt of the response. This includes loopback networking, policy and quota checks, PostgreSQL execution state changes, the Payments REST call and its database read, and response serialization.

The fault run stops Payments, sends four sequential reads, compares Prometheus counters before and after, restores the service, waits out the circuit cooldown, and verifies a successful probe. It inserts old execution metadata into the disposable database to test recovery for pre-dispatch, read, and write states. Finally it stops Redis, verifies failed readiness and fail-closed quota enforcement, and restores Redis. The harness compares response correlation IDs with their persisted values.

The run used Docker Engine 29.7.2 and Compose 5.5.1 on macOS 26.6.2 (arm64), with eight Docker VM CPUs and about 3.82 GiB of VM memory. The client ran under Python 3.12.10. Measurements describe a single local run with synthetic records. They include application latency, and they cannot establish production capacity, gateway-only overhead, tail SLOs, or model workflow latency.

## Results

| Check | Observed result |
|---|---|
| Healthy read concurrency | 20 of 20 succeeded across five batches of four. Median 38.83 ms, nearest-rank p95 153.71 ms, range 28.25–153.76 ms. |
| Payments stopped | Three reads exhausted their attempts. Prometheus recorded six retries and one circuit opening. The fourth read was rejected in 10.51 ms. |
| Payments restored | The probe succeeded after the 15-second cooldown. |
| Stale executions | Seeded pre-dispatch and read rows became `FAILED`. The seeded write became `UNKNOWN`. |
| Redis stopped | Readiness returned 503 and the invocation returned `QUOTA_UNAVAILABLE`. |

An earlier attempt overlapped startup of several Compose projects on the same Docker VM. Four measured reads succeeded, four returned upstream timeouts, and twelve were rejected by the circuit. The published run started one verification project at a time. [Raw samples and fault outcomes](../benchmarks/results/m6-local-operations.json)

## Reproduce

From the repository root with an initialized local `.env` and the locked Python environment, create a fresh disposable stack. The script publishes a sandbox tool and writes execution records. The fault option also stops two dependencies and inserts stale fixtures. Use a fresh `toolmesh-m6-fault` project or a suffixed name such as `toolmesh-m6-fault-repeat`.

```sh
set -a
. ./.env
set +a
export API_PORT=18009 WEB_PORT=15182
docker compose -p toolmesh-m6-fault up --build -d --wait
uv run --locked python -m benchmarks.operations \
  --url http://127.0.0.1:18009 --fault-project toolmesh-m6-fault > m6-operations.json
docker compose -p toolmesh-m6-fault down
```

Omit `--fault-project` for the healthy-load checks alone. A subsequent run needs a fresh project, or enough time for the caller's quota window to expire. Use the runbook for the separate approval timeout case. [Refund timeout rehearsal](runbook.md#reproduce-a-timeout-after-refund-commit)
