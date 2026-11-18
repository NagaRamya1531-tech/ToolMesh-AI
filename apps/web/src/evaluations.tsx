import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { call } from "./api";
import { Screen, State } from "./screen";
import type { Session } from "./workspace";

const strategies = ["keyword", "vector", "hybrid"] as const;
type Strategy = typeof strategies[number];
type Measures = {
  recall_at_5: number | null; precision_at_5: number; mrr: number | null;
  latency_p50_ms: number; latency_p95_ms: number; negative_return_rate: number | null;
  mean_retrieved_schema_bytes: number;
};
type Summary = {
  strategies: Record<Strategy, Measures>; domains: Record<string, Record<Strategy, Measures>>;
  all_allowed_schema_bytes: number; eligible_definitions: number; dataset_fingerprint: string;
  query_embedding_wall_ms: number; index_build_wall_ms: number;
};
type Run = {
  id: string; status: string; stage: string; processed: number; catalog_size: number;
  case_count: number; requested_at: string; configuration: { model: string; split: string; dataset: string; seed: number };
  summary: Summary | null; error_code: string | null;
};
type Example = {id: string; domain: string; kind: string; query: string; expected: string[]; rankings: Record<Strategy,string[]>};
type Detail = {run: Run; misses: Example[]; negative_examples: Example[]; miss_count: number; negative_return_count: number};
const percentage = (value: number | null) => value === null ? "—" : `${(100 * value).toFixed(1)}%`;
const size = (bytes: number) => bytes.toLocaleString(undefined, {maximumFractionDigits: 0});

function Comparison({summary}: {summary: Summary}) {
  return <>
    <h3>Strategy comparison</h3>
    <ul className="outcomes">{strategies.map((strategy) => {
      const value = summary.strategies[strategy].recall_at_5;
      return <li key={strategy}>
        <span>{strategy} Recall@5 · {percentage(value)}</span>
        {value !== null && <meter min={0} max={1} value={value} aria-label={`${strategy} Recall at five`} />}
      </li>;
    })}</ul>
    <div className="table-wrap"><table>
      <thead><tr><th scope="col">Strategy</th><th scope="col">Recall@5</th><th scope="col">Precision@5</th>
        <th scope="col">MRR</th><th scope="col">Negative return rate</th><th scope="col">SQL p50 / p95</th>
        <th scope="col">Mean schema bytes</th></tr></thead>
      <tbody>{strategies.map((strategy) => {
        const metrics = summary.strategies[strategy];
        return <tr key={strategy}>
          <th scope="row">{strategy}</th><td>{percentage(metrics.recall_at_5)}</td>
          <td>{percentage(metrics.precision_at_5)}</td><td>{metrics.mrr?.toFixed(3) ?? "—"}</td>
          <td>{percentage(metrics.negative_return_rate)}</td>
          <td>{metrics.latency_p50_ms.toFixed(2)} / {metrics.latency_p95_ms.toFixed(2)} ms</td>
          <td>{size(metrics.mean_retrieved_schema_bytes)}</td>
        </tr>;
      })}</tbody>
    </table></div>
    <details className="evaluation-details">
      <summary>Recall@5 by domain</summary>
      <div className="table-wrap"><table>
        <thead><tr><th scope="col">Domain</th>{strategies.map((strategy) => <th key={strategy} scope="col">{strategy}</th>)}</tr></thead>
        <tbody>{Object.entries(summary.domains).map(([domain, measures]) => <tr key={domain}>
          <th scope="row">{domain}</th>{strategies.map((strategy) => <td key={strategy}>{percentage(measures[strategy].recall_at_5)}</td>)}
        </tr>)}</tbody>
      </table></div>
    </details>
    <p className="description">Full eligible catalog · {size(summary.all_allowed_schema_bytes)} serialized schema bytes across {size(summary.eligible_definitions)} definitions. No LLM run or provider token count.</p>
    <p className="description">SQL timings exclude query encoding. Batch query encoding took {summary.query_embedding_wall_ms.toFixed(1)} ms, and index construction took {summary.index_build_wall_ms.toFixed(1)} ms.</p>
  </>;
}

function Examples({title,cases}: {title:string;cases:Example[]}) {
  if (!cases.length) return <p>No {title.toLowerCase()} recorded.</p>;
  return <details className="evaluation-details">
    <summary>{title} · showing {cases.length}</summary>
    {cases.map((example) => <div className="example" key={example.id}>
      <p>{example.query}</p>
      <p className="description">{example.domain} · {example.kind}</p>
      <p>Expected <code>{example.expected.join(", ") || "no eligible result"}</code></p>
      {strategies.map((strategy) => <p key={strategy}>{strategy} <code>{example.rankings[strategy].join(", ") || "no results"}</code></p>)}
    </div>)}
  </details>;
}

export function Evaluations({session}: {session: Session}) {
  const admin=session.id === "platform-admin";
  const [params,setParams]=useSearchParams();
  const selected=params.get("run");
  const [pending,setPending]=useState(false);
  const [exporting,setExporting]=useState(false);
  const [error,setError]=useState("");
  const cache=useQueryClient();
  const list=useQuery({
    queryKey:["evaluations",session.id],
    queryFn:()=>call<{model:{id:string;status:string};runs:Run[]}>("/evaluations",session.token),
    enabled:admin,retry:false,refetchInterval:2500,
  });
  const detail=useQuery({
    queryKey:["evaluations",session.id,selected],
    queryFn:()=>call<Detail>(`/evaluations/runs/${encodeURIComponent(selected!)}`,session.token),
    enabled:admin&&!!selected,retry:false,
    refetchInterval:(query)=>["QUEUED","RUNNING"].includes(query.state.data?.run.status ?? "")?1500:false,
  });
  if (!admin) return <Screen name="Evaluations" description="The platform administrator can run and export benchmark results." />;
  const active=list.data?.runs.some((item)=>["QUEUED","RUNNING"].includes(item.status));
  const run=detail.data?.run;
  const denominator=run?.stage==="INDEXING"?run.catalog_size:run?.case_count??1;

  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form=new FormData(event.currentTarget);
    setPending(true);setError("");
    try {
      const result=await call<{id:string}>("/evaluations/runs",session.token,{
        catalog_size:Number(form.get("catalog")),case_count:Number(form.get("cases")),split:form.get("split"),
      });
      setParams({run:result.id});
      await cache.invalidateQueries({queryKey:["evaluations",session.id]});
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Evaluation submission failed.");
    } finally { setPending(false); }
  }

  async function download(format:"json"|"csv") {
    if (!run) return;
    setExporting(true);setError("");
    try {
      const response=await fetch(`/api/v1/evaluations/runs/${run.id}/export?format=${format}`,{
        headers:{Authorization:`Bearer ${session.token}`},
      });
      if (!response.ok) throw new Error("The evaluation export could not be downloaded.");
      const url=URL.createObjectURL(await response.blob());
      const link=document.createElement("a");
      link.href=url;link.download=`toolmesh-evaluation-${run.id}.${format}`;link.click();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Export failed.");
    } finally { setExporting(false); }
  }

  return <Screen name="Evaluations" description="Compare keyword, vector, and hybrid retrieval on a synthetic, non-executable catalog.">
    <State loading={list.isPending} error={list.error} retry={list.refetch}>
      <form className="panel form-panel" onSubmit={start}>
        <label>Catalog definitions<input name="catalog" type="number" min={60} max={10000} defaultValue={1000} required /></label>
        <label>Labeled cases<input name="cases" type="number" min={10} max={1000} defaultValue={120} required /></label>
        <label>Dataset split<select name="split"><option value="test">Held-out test</option><option value="tuning">Tuning</option></select></label>
        <p className="description">{list.data?.model.status==="ready" ? "The pinned local embedding model is ready."
          : "Provision the pinned model using the README before running an evaluation."}</p>
        <button disabled={pending||active||list.data?.model.status!=="ready"}>{active?"Evaluation in progress":"Run evaluation"}</button>
      </form>
      {error && <p className="warning" role="alert">{error}</p>}
      {list.data?.runs.length ? <div className="panel table-wrap detail"><table>
        <thead><tr><th scope="col">Requested</th><th scope="col">Split</th><th scope="col">Definitions / cases</th><th scope="col">Status</th></tr></thead>
        <tbody>{list.data.runs.map((item)=><tr key={item.id}>
          <th scope="row"><Link to={`?run=${item.id}`}>{new Date(item.requested_at).toLocaleString()}</Link></th>
          <td>{item.configuration.split}</td><td>{size(item.catalog_size)} / {size(item.case_count)}</td><td>{item.status}</td>
        </tr>)}</tbody>
      </table></div> : <p>No evaluation runs yet.</p>}
    </State>
    {selected && <section className="panel detail" aria-label="Evaluation run">
      <h2>Evaluation run</h2>
      <State loading={detail.isPending} error={detail.error} retry={detail.refetch}>
        {run && <>
          <p role="status">{run.status}{run.error_code&&` · ${run.error_code}`}</p>
          {run.status==="RUNNING" && <p><label>{run.stage} · {run.processed} / {denominator} <progress value={run.processed} max={denominator} /></label></p>}
          <p className="description">{run.configuration.dataset} · {run.configuration.split} · seed {run.configuration.seed}</p>
          {run.summary && <>
            <div className="inline-controls"><button disabled={exporting} onClick={()=>void download("json")}>Download JSON</button>
              <button disabled={exporting} onClick={()=>void download("csv")}>Download CSV</button></div>
            <Comparison summary={run.summary} />
            <Examples title={`Labeled misses (${detail.data!.miss_count})`} cases={detail.data!.misses} />
            <Examples title={`Returned candidates for negative cases (${detail.data!.negative_return_count})`} cases={detail.data!.negative_examples} />
            <details className="evaluation-details"><summary>Reproducibility</summary>
              <p>Model <code>{run.configuration.model}</code></p><p>Dataset SHA-256 <code>{run.summary.dataset_fingerprint}</code></p>
            </details>
          </>}
        </>}
      </State>
    </section>}
  </Screen>;
}
