# v0.1.0 release validation

This release verifies the ToolMesh sandbox with real PostgreSQL, Redis, REST, MCP and gRPC services. The shipping console runs the four scripted LangGraph profiles. The separate provider harness measures real local model selection over synthetic sandbox metadata.

## Reproduce

Follow the [fresh install and test commands](../README.md#development-and-testing). Use a separate Compose project with a fresh database for each browser, Python, and benchmark run. Provision the pinned embedding model before starting the gateway or restart the API after downloading it.

For the evaluation, start another fresh project from the repository root. Install and start Ollama locally with an available model such as `gemma:2b`. The runner accepts loopback only and publishes synthetic fixture tools in the target sandbox.

```sh
API_PORT=18108 WEB_PORT=15188 docker compose -p toolmesh-release-eval up --build -d --wait
uv run --locked python -m benchmarks.release --gateway http://127.0.0.1:18108 \
  --model gemma:2b --output m7-local-release.json
uv run --locked python -m benchmarks.run --url http://127.0.0.1:18108 \
  --definitions 1000 --cases 120 --output m7-seed42-1000x120.json.gz
docker compose -p toolmesh-release-eval down
```

For the approval delay rehearsal, see [timeout after refund commit](runbook.md#reproduce-a-timeout-after-refund-commit). For a reviewer demo, follow [the duplicate-payment workflow](runbook.md#run-the-duplicate-payment-workflow), then open the persisted execution timeline and audit view.

## Method

- **Retrieval.** Use the existing held-out synthetic corpus and labels, seed 42, 1,000 definitions, 120 cases, and three ranked strategies. The synthetic definitions cannot execute.
- **Provider selection.** Ask Ollama to select one tool on each of four labeled tasks for Support, Finance, IT and HR. Compare all visible business-tool schemas upfront with top-five keyword, vector, and hybrid summaries. Use temperature zero, seed 42, an 8,192-token configured window and one warm-up. The structured response constrains selection to the visible candidates or abstention. Once the model selects the correct tool, the harness uses fixed fixture arguments and verifies a read-only result through the real gateway. The harness stores provider-reported token counters and duration, but omits reasoning and free-text output. [Ollama generate API](https://docs.ollama.com/api/generate)
- **Load.** Submit 100 simultaneous authenticated hybrid searches after one warm-up. Record the response status and client wall time of each request. The burst includes HTTP and database queueing, ranking and serialization. It excludes generation and tool execution and does not measure capacity under sustained load.

The four provider cases measure tool selection plus verified read results. They do not measure model-generated arguments, approval workflows, or arbitrary multi-step planning. The upfront schema baseline covers the scoped runnable catalog. A full 1,000-definition provider context, provider-driven Playground and gateway-only latency remain unmeasured. Reported usage comes from the provider; local monetary cost was not measured.

The local environment used macOS arm64, Docker Engine 29.7.2 with eight VM CPUs and approximately 3.82 GiB VM memory, and the provider and model versions in the raw export. CPU and memory saturation during the burst were not instrumented.

## Measured results

### Held-out retrieval

The run contained 72 positive and 48 negative cases. SQL timing measures the client-to-database round trip for each ranking query.

| Strategy | Recall@5 | MRR | Precision@5 | SQL p95 |
|---|---:|---:|---:|---:|
| Keyword | 80.6% | 0.681 | 9.7% | 6.95 ms |
| Vector | 90.3% | 0.794 | 10.8% | 20.26 ms |
| Hybrid | 94.4% | 0.793 | 11.3% | 23.81 ms |

Hybrid fell short of the 95% Recall@5 target. Every strategy returned candidates for all 48 negative cases. The raw export includes per-case outcomes and role breakdowns. [Held-out export](../benchmarks/results/m7-seed42-1000x120.json.gz)

### Local model and load

Ollama 0.40.16 ran `gemma:2b` with GPU offload. Token counts are sums over four trials per strategy. Timing covers all trials, including wrong selections.

| Strategy | Correct read results | Input tokens | Total tokens | Median provider wall time | Median trial wall time |
|---|---:|---:|---:|---:|---:|
| Upfront schemas | 0/4 | 10,329 | 10,379 | 7,085.75 ms | 7,163.67 ms |
| Keyword | 2/4 | 603 | 649 | 647.70 ms | 1,669.43 ms |
| Vector | 2/4 | 647 | 693 | 522.10 ms | 659.44 ms |
| Hybrid | 2/4 | 651 | 697 | 464.58 ms | 515.36 ms |

The ranked strategies succeeded for Finance and HR. All strategies selected the wrong tool for Support and IT. In particular, the ranked IT trials picked the restart tool and the runner stopped before execution. The sample falls short of the 90% task success target and is too small to estimate general model accuracy.

The isolated 100-client burst returned 100 HTTP 200 responses at 166.82 requests per second over 0.60 s. Client p50, p95 and p99 were 331.87, 567.08 and 589.42 ms. This is a single warm discovery burst on the small executable catalog. An earlier burst also returned 100 HTTP 200 responses, after which the gateway timed out before catalog retrieval in all 16 model trials. The rerun restarted Docker Desktop and ran one Compose project at a time. [Isolated results](../benchmarks/results/m7-local-release.json) · [Initial attempt](../benchmarks/results/m7-initial-attempt.json)

## Optional OpenAI comparison

The runner accepts `--openai --model MODEL` for a model that supports Structured Outputs. Set `OPENAI_API_KEY` in the root `.env` or the invoking process environment. OpenAI mode makes up to 16 billable model requests and sends the synthetic task text and scoped tool metadata to the Responses API. The fixed fixture arguments and gateway credentials stay local. The export records the usage object returned by the provider. OpenAI uses its own model defaults, so compare latency and token counts with the provider and settings in mind. The live comparison is recorded below. [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)

### GPT-5.4 mini follow-up

The pinned `gpt-5.4-mini-2026-03-17` run completed all 16 requests.

| Strategy | Verified reads | Input tokens | Output tokens | Median provider wall time |
|---|---:|---:|---:|---:|
| Upfront schemas | 3/4 | 10,009 | 61 | 1,203.68 ms |
| Keyword | 3/4 | 660 | 67 | 1,078.10 ms |
| Vector | 3/4 | 708 | 61 | 749.44 ms |
| Hybrid | 3/4 | 712 | 73 | 1,014.02 ms |

OpenAI verified all Support, Finance, and HR reads. The expected IT health tool was absent from the visible catalog in both provider runs. OpenAI abstained in all four IT trials. All methods therefore verified every available case in the OpenAI run. The historical IT trials measure behavior with a missing tool. The runner now crawls the IT service if its health contract is missing, publishes the fixture contracts, and checks visibility under all four roles before model requests. See the [corrected M8 provider run](#m8-corrected-provider-validation) below.

The provider reported 12,089 input tokens, zero cached input tokens, and 262 output tokens. Estimated inference cost was $0.01025 USD at standard list rates. [Raw OpenAI comparison](../benchmarks/results/m7-openai-comparison.json) · [Model pricing](https://developers.openai.com/api/docs/models/gpt-5.4-mini)

## M8 corrected provider validation

M8 used the same four tasks and pinned GPT-5.4 mini snapshot on a disposable Compose project. The harness crawled and published the previously missing IT health tool and verified visibility for every test identity before calling the provider.

| Strategy | Verified reads | Input tokens | Output tokens |
|---|---:|---:|---:|
| Upfront schemas | 4/4 | 10,705 | 64 |
| Keyword | 4/4 | 710 | 64 |
| Vector | 4/4 | 756 | 64 |
| Hybrid | 4/4 | 760 | 70 |

The corrected benchmark completed 16 provider requests across four fixed tasks. It reported 12,931 input tokens including 7,168 cached tokens, and 262 output tokens. Estimated list-rate cost was $0.00604 USD. The four provider Playground tasks verified their expected fixture reads across Support, Finance, IT and HR. Support was rerun after an accessibility fix. The 21 requests across both validations cost an estimated $0.0090 USD. These four synthetic cases do not establish production model accuracy. [Corrected benchmark](../benchmarks/results/m8-openai-corrected.json) · [Finance, IT and HR API evidence](../benchmarks/results/m8-playground-api.json) · [Live Support console capture](screenshots/m8-playground.png) · [Model pricing](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
