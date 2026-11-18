import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { call } from "./api";
import { ServiceActions, ServiceRegistration } from "./crawler";
import { Screen, State } from "./screen";
export { Screen, State } from "./screen";

export type Session = { token: string; id: string; kind: "human" | "agent" };
type Service = { id: string; name: string; protocol: string; owner: string; published_tools: number; health: string; checked_at: string|null; health_error: string|null };
type Tool = { id: string; service_id: string; version: number; description: string; risk: string };
type Contract = { description: string; input_schema: object; output_schema: object; risk: string };
type Definition = { id: string; service_id: string; version: number; active_version: number | null; lifecycle: string; contract: Contract };
type Run = { id: string; status: string; error_code: string | null; result: unknown; approval_id?: string | null };
type Execution = Omit<Run, "result"> & {
  principal: string; tool_id: string | null; version: number | null;
  started_at: string; finished_at: string | null;
};

function message(error: unknown) {
  return error instanceof Error ? error.message : "The request failed.";
}

export function SignIn({ onSession }: { onSession: (session: Session) => void }) {
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setPending(true);
    setError("");
    try {
      const { access_token } = await call<{ access_token: string }>("/auth/login", undefined, {
        username: form.get("username"), password: form.get("password"),
      });
      const who = await call<Omit<Session, "token">>("/auth/me", access_token);
      onSession({ token: access_token, ...who });
    } catch (failure) {
      setError(message(failure));
    } finally {
      setPending(false);
    }
  }
  return <Screen name="Sign in" description="Use a local sandbox identity to access the catalog and runtime.">
    <form className="panel form-panel" onSubmit={submit}>
      <label>Identity<select name="username">
        <option value="platform-admin">Platform administrator</option>
        <option value="support-agent">Support agent</option>
        <option value="payments-agent">Payments agent</option>
        <option value="finance-agent">Finance agent</option>
        <option value="refund-approver">Refund approver</option><option value="it-agent">IT agent</option>
      </select></label>
      <label>Password<input name="password" type="password" autoComplete="current-password" maxLength={256} required /></label>
      {error && <p role="alert" className="warning">{error}</p>}
      <button disabled={pending}>{pending ? "Signing in…" : "Sign in"}</button>
    </form>
  </Screen>;
}

export function Services({ session }: { session: Session }) {
  const [params, setParams] = useSearchParams();
  const protocol = params.get("protocol") ?? "";
  const selected = params.get("service");
  const query = useQuery({
    queryKey: ["services", session.id], queryFn: () => call<Service[]>("/services", session.token),
  });
  const values = query.data?.filter((service) => !protocol || service.protocol === protocol) ?? [];
  return <Screen name="Services" description="Inspect registered sandbox services and their published tool counts.">
    <label className="filter">Protocol <select value={protocol} onChange={(event) => setParams(event.target.value ? { protocol: event.target.value } : {})}>
      <option value="">All protocols</option><option>REST</option><option>MCP</option><option>gRPC</option>
    </select></label>
    {session.id==="platform-admin" && query.data && <ServiceRegistration session={session} registered={query.data.map((option)=>option.id)} />}
    <State loading={query.isPending} error={query.error} retry={query.refetch}>
      {values.length === 0 ? <p>No services match this filter.</p> : <div className="panel table-wrap"><table>
        <thead><tr><th scope="col">Service</th><th scope="col">Protocol</th><th scope="col">Published tools</th><th scope="col">Health</th></tr></thead>
        <tbody>{values.map((service) => <tr key={service.id}>
          <th scope="row"><Link to={`?protocol=${protocol}&service=${service.id}`}>{service.name}</Link></th>
          <td>{service.protocol}</td><td>{service.published_tools}</td><td>{service.health}{service.checked_at && Date.now()-Date.parse(service.checked_at)>300000 ? " · stale" : ""}</td>
        </tr>)}</tbody>
      </table></div>}
      {values.filter((service) => service.id === selected).map((service) => <section className="panel detail" key={service.id}>
        <h2>{service.name}</h2><p>Owner <code>{service.owner}</code></p><p>Endpoint <code>{service.id}:{service.protocol==="gRPC" ? 50051 : 8000}</code></p>
        <Link to={`/tools?service=${service.id}`}>View tools</Link>
        {service.checked_at && <p>Last checked {new Date(service.checked_at).toLocaleString()} {service.health_error ?? ""}</p>}
        {session.id==="platform-admin" && <ServiceActions key={service.id} session={session} service={service.id} name={service.name}/>}
      </section>)}
    </State>
  </Screen>;
}

function schemaChanges(before: object, after: object) {
  const old=before as {properties?:Record<string, unknown>;required?:string[]};
  const next=after as {properties?:Record<string, unknown>;required?:string[]};
  const previous=old.properties ?? {};
  const current=next.properties ?? {};
  return {
    added:Object.keys(current).filter((key)=>!(key in previous)),
    removed:Object.keys(previous).filter((key)=>!(key in current)),
    changed:Object.keys(current).filter((key)=>key in previous&&JSON.stringify(current[key])!==JSON.stringify(previous[key])),
    required_added:(next.required??[]).filter((key)=>!(old.required??[]).includes(key)),
    required_removed:(old.required??[]).filter((key)=>!(next.required??[]).includes(key)),
  };
}

export function Registry({ session }: { session: Session }) {
  const [params, setParams] = useSearchParams();
  const service = params.get("service") ?? "";
  const selected = params.get("tool");
  const version = params.get("version");
  const [reason, setReason] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const client = useQueryClient();
  const admin = session.id === "platform-admin";
  const query = useQuery({
    queryKey: ["registry", session.id],
    queryFn: () => call<(Tool | Definition)[]>("/tools", session.token),
  });
  const matching = query.data?.filter((tool) => !service || tool.service_id === service) ?? [];
  const values = matching.filter((entry,index)=>matching.findIndex((value)=>value.id===entry.id)===index);
  const choices = matching.filter((item)=>item.id===selected);
  const tool = choices.find((item)=>String(item.version)===version) ?? choices[0];
  const history = useQuery({
    queryKey:["release-history",selected,session.id],
    queryFn:()=>call<{history:{action:string;actor:string;version:number;reason:string;created_at:string}[]}>(`/tools/${encodeURIComponent(selected!)}/versions`,session.token),
    enabled:admin&&!!selected,
  });
  const detail = useQuery({
    queryKey: ["schema", session.id, selected],
    queryFn: () => call<Definition>(`/discovery/tools/${encodeURIComponent(selected!)}/schema`, session.token),
    enabled: !!tool && !("contract" in tool),
  });
  const contract = tool && "contract" in tool ? tool.contract : detail.data?.contract;
  const previous = choices.find((item)=>tool && item.version===tool.version-1);
  const diff = previous && "contract" in previous && contract ? {
    input:schemaChanges(previous.contract.input_schema,contract.input_schema),
    output:schemaChanges(previous.contract.output_schema,contract.output_schema),
  } : null;
  async function publish(event: FormEvent, definition: Definition, action = "publish") {
    event.preventDefault();
    setPending(true);
    setError("");
    setNotice("");
    try {
      await call(`/tools/${encodeURIComponent(definition.id)}/${action}`, session.token, {
        version: definition.version, reason,
      });
      await Promise.all(["registry", "services", "discovery", "release-history"].map((key) => client.invalidateQueries({ queryKey: [key] })));
      setReason("");
      setNotice(action==="publish" ? `Published ${definition.id} v${definition.version}.` : `Recorded ${action} for ${definition.id} v${definition.version}.`);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setPending(false);
    }
  }
  return <Screen name="Registry" description={admin ? "Review immutable drafts and publish approved tool contracts." : "Inspect published tool contracts within your assigned scope."}>
    <label className="filter">Service <select value={service} onChange={(event) => setParams(event.target.value ? { service: event.target.value } : {})}>
      <option value="">All services</option><option value="payments">Payments</option><option value="support">Support</option><option value="communication">Communication</option><option value="inventory">Inventory</option><option value="it">IT Operations</option>
    </select></label>
    {notice && <p role="status" className="ok">{notice}</p>}
    <State loading={query.isPending} error={query.error} retry={query.refetch}>
      {values.length === 0 ? <p>No tools match this filter.</p> : <div className="panel table-wrap"><table>
        <thead><tr><th scope="col">Tool</th><th scope="col">Version</th><th scope="col">Status</th></tr></thead>
        <tbody>{values.map((value) => <tr key={value.id}><th scope="row"><Link to={`?service=${service}&tool=${value.id}`} onClick={() => {setNotice(""); setError(""); setReason("");}}>{value.id}</Link></th>
          <td>v{value.version}</td><td>{"lifecycle" in value
            ? value.lifecycle !== "published"
              ? value.lifecycle[0].toUpperCase() + value.lifecycle.slice(1)
              : value.active_version === value.version ? "Published" : `Draft · active v${value.active_version}`
            : "Published"}</td></tr>)}</tbody>
      </table></div>}
    </State>
    {tool && <section className="panel detail" aria-label="Tool contract">
      <h2>{tool.id}</h2>
      {choices.length>1 && <label className="filter">Contract version <select value={tool.version} onChange={(event)=>setParams({service,tool:tool.id,version:event.target.value})}>
        {choices.map((entry)=><option key={entry.version} value={entry.version}>v{entry.version}</option>)}
      </select></label>}
      {"contract" in tool && tool.lifecycle!=="archived" &&
        <form className="form-panel compact" onSubmit={(event) => {
          const action=event.nativeEvent instanceof SubmitEvent ? (event.nativeEvent.submitter as HTMLButtonElement|null)?.value : undefined;
          void publish(event,tool,action || "publish");
        }}>
          <label>Publication reason<input value={reason} maxLength={240} required onChange={(event) => setReason(event.target.value)} /></label>
          {error && <p role="alert" className="warning">{error}</p>}
          <div className="inline-controls">
            {tool.active_version!==tool.version && <button value="publish" disabled={pending || !reason.trim()}>Publish version</button>}
            {tool.active_version===null && <button value="review" disabled={pending || !reason.trim()}>Mark reviewed</button>}
            {tool.active_version!==null && <button value="disable" disabled={pending || !reason.trim()}>Disable tool</button>}
            {tool.active_version!==null && tool.version<tool.active_version && <button value="rollback" disabled={pending || !reason.trim()}>Roll back</button>}
            <button value="archive" disabled={pending || !reason.trim()}>Archive tool</button>
          </div>
        </form>}
      <State loading={!contract && detail.isPending} error={detail.error} retry={detail.refetch}>
        {contract && <><p>{contract.description} <span className="environment">{contract.risk}</span></p>
          <h3>Input schema</h3><pre>{JSON.stringify(contract.input_schema, null, 2)}</pre>
          <h3>Output schema</h3><pre>{JSON.stringify(contract.output_schema, null, 2)}</pre></>}
      </State>
      {diff && <details><summary>Schema diff from v{previous?.version}</summary><pre>{JSON.stringify(diff,null,2)}</pre></details>}
      {history.data && history.data.history.length>0 && <details>
        <summary>Release history</summary>
        <ul>{history.data.history.map((event,index)=><li key={`${event.created_at}-${index}`}>
          {event.action} v{event.version} · {event.actor} · {event.reason}
        </li>)}</ul>
      </details>}
    </section>}
  </Screen>;
}

export function Playground({ session }: { session: Session }) {
  const [selected, setSelected] = useState("");
  const [argumentsText, setArguments] = useState("{}");
  const [requestKey, setRequestKey] = useState(() => crypto.randomUUID());
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const client = useQueryClient();
  const tools = useQuery({
    queryKey: ["discovery", session.id],
    queryFn: () => call<Tool[]>("/discovery/search", session.token, { query: "", limit: 50 }),
  });
  const schema = useQuery({
    queryKey: ["schema", session.id, selected],
    queryFn: () => call<Definition>(`/discovery/tools/${encodeURIComponent(selected)}/schema`, session.token),
    enabled: !!selected,
  });
  async function invoke(event: FormEvent) {
    event.preventDefault();
    if (!schema.data) return;
    setError("");
    setRun(null);
    let args: unknown;
    try { args = JSON.parse(argumentsText); } catch { setError("Enter valid JSON arguments."); return; }
    if (!args || typeof args !== "object" || Array.isArray(args)) {
      setError("Arguments must be a JSON object.");
      return;
    }
    setPending(true);
    try {
      setRun(await call<Run>("/executions", session.token, {
        tool_id: selected, version: schema.data.version, arguments: args,
        ...(schema.data.contract.risk === "approval" ? { idempotency_key: requestKey } : {}),
      }));
      await client.invalidateQueries({ queryKey: ["executions"] });
    } catch (failure) {
      setError(message(failure));
    } finally { setPending(false); }
  }
  return <Screen name="Playground" description="Run one published sandbox tool using the signed-in identity.">
    <p><Link to="/playground/duplicate-payment">Open scripted duplicate-payment workflow</Link></p>
    <State loading={tools.isPending} error={tools.error} retry={tools.refetch}>
      {tools.data?.length === 0 && <p>No published tools are available to this identity.</p>}
      <form className="panel form-panel" onSubmit={invoke}>
        <label>Tool<select value={selected} onChange={(event) => {setSelected(event.target.value); setRequestKey(crypto.randomUUID()); setRun(null); setError("");}} disabled={pending}>
          <option value="">Choose a tool</option>{tools.data?.map((tool) => <option value={tool.id} key={tool.id}>{tool.id}</option>)}
        </select></label>
        {selected && <State loading={schema.isPending} error={schema.error} retry={schema.refetch}>
          {schema.data && <details><summary>Input contract · v{schema.data.version}</summary><pre>{JSON.stringify(schema.data.contract.input_schema, null, 2)}</pre></details>}
        </State>}
        <label>Arguments<textarea spellCheck={false} value={argumentsText} onChange={(event) => {setArguments(event.target.value);setRequestKey(crypto.randomUUID());}} disabled={pending} /></label>
        {error && <p role="alert" className="warning">{error}</p>}
        <button disabled={!schema.data || pending}>{pending ? "Invoking…" : "Invoke tool"}</button>
      </form>
    </State>
    {run && <section className="panel detail"><h2>Execution result</h2>
      <p role="status">{run.status}{run.error_code ? ` · ${run.error_code}` : ""}</p>
      <Link to={`/executions?trace=${run.id}`}>Open execution trace</Link>
      {run.approval_id && <p><Link to={`/approvals?approval=${run.approval_id}`}>Open refund approval</Link></p>}
      {run.result !== null && <pre>{JSON.stringify(run.result, null, 2)}</pre>}
    </section>}
  </Screen>;
}

export function Executions({ session }: { session: Session }) {
  const [params, setParams] = useSearchParams();
  const trace = params.get("trace");
  const status = params.get("status") ?? "";
  const hours = params.get("hours") ?? "";
  const filters = new URLSearchParams();
  if (status) filters.set("status", status);
  if (hours) filters.set("hours", hours);
  const query = useQuery({
    queryKey: ["executions", session.id, status, hours], queryFn: () => call<Execution[]>(`/executions?${filters}`, session.token),
  });
  const detail = useQuery({
    queryKey: ["executions", session.id, trace],
    queryFn: () => call<Execution>(`/executions/${encodeURIComponent(trace!)}`, session.token),
    enabled: !!trace,
  });
  const list = query.data ?? [];
  return <Screen name="Executions" description="Review the latest persisted invocation outcomes and denial evidence.">
    <div className="inline-controls"><label>Status <select value={status} onChange={(event) => setParams({ status: event.target.value, ...(hours ? {hours} : {}) })}>
      <option value="">All statuses</option>{["SUCCEEDED", "DENIED", "FAILED", "UNKNOWN", "RECEIVED", "RUNNING", "PENDING_APPROVAL", "REJECTED", "EXPIRED"].map((value) => <option key={value}>{value}</option>)}
    </select></label><button disabled={query.isFetching || detail.isFetching} onClick={() => {void query.refetch(); if (trace) void detail.refetch();}}>Refresh</button></div>
    <State loading={query.isPending} error={query.error} retry={query.refetch}>
      {list.length === 0 ? <p>No executions match this filter.</p> : <div className="panel table-wrap"><table>
        <thead><tr><th scope="col">Trace</th><th scope="col">Tool</th><th scope="col">Outcome</th></tr></thead>
        <tbody>{list.map((run) => <tr key={run.id}><th scope="row"><Link to={`?${filters}&trace=${run.id}`}>{run.id.slice(0, 8)}</Link></th><td className="verbatim">{run.tool_id ?? "Unavailable"}</td><td>{run.status}</td></tr>)}</tbody>
      </table></div>}
    </State>
    {trace && <section className="panel detail"><h2>Execution trace</h2>
      <State loading={detail.isPending} error={detail.error} retry={detail.refetch}>
        {detail.data && <dl>
          <dt>Trace ID</dt><dd><code>{detail.data.id}</code></dd>
          <dt>Caller</dt><dd>{detail.data.principal}</dd><dt>Tool version</dt><dd>{detail.data.version ?? "Unavailable"}</dd>
          <dt>Outcome</dt><dd>{detail.data.status}{detail.data.error_code ? ` · ${detail.data.error_code}` : ""}</dd>
          <dt>Started</dt><dd>{new Date(detail.data.started_at).toLocaleString()}</dd>
          <dt>Finished</dt><dd>{detail.data.finished_at ? new Date(detail.data.finished_at).toLocaleString() : "Pending"}</dd>
        </dl>}
      </State>
    </section>}
  </Screen>;
}
