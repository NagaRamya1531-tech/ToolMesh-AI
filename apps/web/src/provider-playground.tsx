import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { call } from "./api";
import { State } from "./screen";

type ProviderConfig = { available: boolean; model: string };
type ProviderRun = {
  model: string;
  status: string;
  tool?: string;
  candidate_count?: number;
  usage?: { input_tokens: number | null; output_tokens: number | null; estimated_usd?: number };
  execution?: { id: string; error_code: string | null; result: object | null };
};

export function ProviderPlayground({ token, principal }: { token: string; principal: string }) {
  const client = useQueryClient();
  const config = useQuery({
    queryKey: ["playground-provider", principal],
    queryFn: () => call<ProviderConfig>("/playground/provider", token),
  });
  const run = useMutation({
    mutationFn: (body: object) => call<ProviderRun>("/playground/provider", token, body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["executions"] }),
  });
  const result = run.data;
  return <div className="provider-workspace">
    <section className="panel provider-panel">
      <header className="provider-header">
        <div><h2>OpenAI sandbox</h2><p>Let the model select one published read for this identity.</p></div>
        {config.data && <span className={config.data.available ? "provider-ready" : "provider-offline"}>
          {config.data.available ? "Configured" : "Disabled"}
        </span>}
      </header>
      <State loading={config.isPending} error={config.error} retry={config.refetch}>
        {config.data?.available ? <form className="provider-form" onSubmit={(event) => {
          event.preventDefault();
          const fields = new FormData(event.currentTarget);
          run.mutate({ prompt: fields.get("prompt"), search: fields.get("search"), strategy: fields.get("strategy") });
        }}>
          <label htmlFor="provider-prompt">Sandbox task</label>
          <textarea id="provider-prompt" name="prompt" maxLength={600} required placeholder="Check for duplicate payments on order O-102." />
          <div className="provider-fields">
            <label>Catalog search<input name="search" maxLength={128} placeholder="e.g. duplicate payment" /></label>
            <label>Discovery strategy<select name="strategy">
              <option value="keyword">Keyword</option><option value="vector">Vector</option><option value="hybrid">Hybrid</option>
            </select></label>
          </div>
          <p className="provider-note">Use synthetic data. The task and candidate schemas go to OpenAI. A request may incur API charges.</p>
          <button className="provider-submit" disabled={run.isPending}>{run.isPending ? "Running sandbox task…" : "Run read-only task"}</button>
        </form> : <p className="provider-empty">An operator can enable this mode using the gateway’s OpenAI configuration.</p>}
      </State>
    </section>
    <aside className="panel provider-panel" aria-label="Provider execution">
      <header className="provider-header"><div><h2>Execution details</h2>
        {config.data && <p className="provider-model">{config.data.model}</p>}</div></header>
      {run.isError && <p className="provider-empty warning" role="alert">{run.error.message}</p>}
      {run.isPending && <p className="provider-empty" role="status">Selecting and validating a read operation.</p>}
      {!run.isPending && !run.isError && !result && <p className="provider-empty">Run a task to see its selected tool, trace and usage.</p>}
      {result && <div className="provider-result">
        <p role="status">{result.status.replaceAll("_", " ")}{result.execution?.error_code ? ` · ${result.execution.error_code}` : ""}</p>
        <dl>
          {result.tool && <><dt>Selected tool</dt><dd><code>{result.tool}</code></dd></>}
          {result.candidate_count !== undefined && <><dt>Candidate tools</dt><dd>{result.candidate_count}</dd></>}
          {result.usage && <>
            <dt>Input / output tokens</dt><dd>{result.usage.input_tokens ?? "Unavailable"} / {result.usage.output_tokens ?? "Unavailable"}</dd>
            <dt>Estimated cost</dt><dd>{result.usage.estimated_usd === undefined ? "Unavailable" : `$${result.usage.estimated_usd.toFixed(5)} USD`}</dd>
          </>}
          {result.execution && <><dt>Trace</dt><dd><Link to={`/executions?trace=${result.execution.id}`}>{result.execution.id}</Link></dd></>}
        </dl>
        {result.execution?.result && <pre tabIndex={0} aria-label="Tool result">{JSON.stringify(result.execution.result, null, 2)}</pre>}
      </div>}
    </aside>
  </div>;
}
