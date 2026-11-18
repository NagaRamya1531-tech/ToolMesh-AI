# M4 discovery evaluation

## Method

The retrieval-only benchmark uses 10,000 generated definitions, 1,000 held-out cases and seed 42. It covers Payments, Support, IT, HR, Analytics and Communication. The synthetic organization and region groups in the tuning and held-out splits are disjoint. The held-out set includes manually written paraphrases, lexical queries, denied-domain queries and unanswerable queries. The ranking parameters were fixed before this run. The case labels are explicit and the dataset SHA-256 is in the export.

Keyword retrieval uses weighted PostgreSQL full-text rank. Vector retrieval uses exact pgvector cosine distance with the pinned 256-dimensional Potion model. Hybrid retrieval fuses the two rankings using a reciprocal-rank constant of 60 and at most 100 candidates per strategy. Each case's eligible document set is materialized before scoring. Strategy order rotates within the single-worker run to spread warm-cache effects.

Recall@5 and MRR are averaged over cases with an eligible relevant definition. Precision@5 uses a denominator of five and includes negative cases. The negative return rate measures how often a case with no eligible answer returned any candidates. SQL p50 and p95 include the PostgreSQL round trip for each strategy. They exclude query embedding, model loading, and agent latency. Schema bytes are the size of compact UTF-8 JSON. The full-catalog baseline uses all generally eligible definitions before the per-case denied-domain restriction.

The host was an Apple M3. Docker Engine 29.7.2 ran Linux ARM64 with eight allocated CPUs and 4.1 GB of VM memory. PostgreSQL was 17.10 with pgvector 0.8.2. This run measures local, sequential retrieval on generated data. Similar names recur across scopes and some question templates repeat across organizations. The benchmark does not measure throughput, provider token use, task completion or generalization to real service descriptions.

## Observed results

| Strategy | Recall@5 | Precision@5 | MRR | SQL p50 | SQL p95 | Negative return rate | Mean schema bytes |
|---|---:|---:|---:|---:|---:|---:|---:|
| Keyword | 71.17% | 8.54% | 0.586 | 39.06 ms | 69.25 ms | 100% | 1,752 |
| Vector | 81.67% | 9.80% | 0.731 | 22.58 ms | 203.17 ms | 100% | 1,751 |
| Hybrid | 89.50% | 10.74% | 0.738 | 54.40 ms | 231.01 ms | 100% | 1,752 |

Hybrid ranked more labeled eligible tools in the top five. It missed the SRS target of 95% Recall@5 and had the highest SQL p95. All three strategies returned candidates for all 400 negative cases. The restricted definitions and denied domains were absent from their respective rankings. A rank alone is insufficient evidence that a suitable tool exists.

The 8,572 generally eligible definitions occupied 3,011,153 serialized bytes. The hybrid top five averaged 1,752 bytes. These figures compare JSON schema payloads. No LLM loaded either payload during this run.

Index construction took 2.54 seconds, including 1.32 seconds of document encoding. Batch query encoding took 111.77 ms. The recorded run took 237.67 seconds end to end. The dataset SHA-256 is `1dc5531db2b5013c856f6764e40598713aba6f270282fd7c87bdd878bb39076a`.

## Reproduce

Provision the model and start the Compose stack as described in the README. Submit a held-out evaluation with the same parameters and compare its case rankings and dataset digest. Timings will vary with the environment.

```sh
uv run --locked python -m benchmarks.run --definitions 10000 --cases 1000 --split test --output /tmp/toolmesh-m4-retrieval.json.gz
```

[Raw case export](../benchmarks/results/m4-seed42-10000x1000.json.gz) · [Pinned model](https://huggingface.co/minishlab/potion-base-8M/tree/bf8b056651a2c21b8d2565580b8569da283cab23)
