import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { call } from "./api";
import { RecordWorkspace, Screen, State, StatusLabel } from "./screen";
import type { Session } from "./workspace";

type Target = { id: string; protocol: string; endpoint: string };
type CrawlChange = { status: string; name?: string; tool_id?: string; version?: number; reason?: string; diff?: Record<string, Record<string, string[] | boolean>> };
type CrawlJob = {
  id: string; service_id: string; status: string; requested_at: string; started_at: string | null;
  finished_at: string | null; actor: string; error: string | null; changes: CrawlChange[];
};
type ServiceOption = { id: string; name: string };

export function ServiceRegistration({ session, registered }: {session: Session; registered: string[]}) {
  const targets = useQuery({queryKey:["service-targets",session.id],queryFn:()=>call<Target[]>("/service-targets",session.token),retry:false});
  const cache = useQueryClient();
  const [target,setTarget] = useState("");
  const [name,setName] = useState("");
  const [error,setError] = useState("");
  const [pending,setPending] = useState(false);
  const available = targets.data?.filter((item)=>!registered.includes(item.id)) ?? [];
  async function register(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError("");
    try {
      await call("/services",session.token,{target,name});
      await cache.invalidateQueries({queryKey:["services"]});
      setTarget("");
      setName("");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Registration failed.");
    } finally {
      setPending(false);
    }
  }
  return <State loading={targets.isPending} error={targets.error} retry={targets.refetch}>
    {available.length > 0 && <details className="panel detail">
      <summary>Register a service</summary>
      <form className="form-panel compact" onSubmit={register}>
        <label>Allowlisted target<select value={target} onChange={(event)=>setTarget(event.target.value)} required>
          <option value="">Choose a target</option>
          {available.map((option)=><option value={option.id} key={option.id}>{option.id} · {option.protocol} · {option.endpoint}</option>)}
        </select></label>
        <label>Display name<input value={name} maxLength={100} onChange={(event)=>setName(event.target.value)} required /></label>
        {error && <p role="alert">{error}</p>}
        <button disabled={pending || !target || !name.trim()}>Register service</button>
      </form>
    </details>}
  </State>;
}

export function ServiceActions({ session, service, name }: {session: Session; service: string; name: string}) {
  const [pending,setPending] = useState(false);
  const [response,setResponse] = useState("");
  const [draft,setDraft] = useState(name);
  const cache=useQueryClient();
  async function testConnection() {
    setPending(true);
    try {
      const result=await call<{health:string;health_error?:string}>(`/services/${service}/test-connection`,session.token,{});
      setResponse(result.health_error ? `${result.health} · ${result.health_error}` : result.health);
      await cache.invalidateQueries({queryKey:["services"]});
    } catch (failure) {
      setResponse(failure instanceof Error ? failure.message : "Connection test failed.");
    } finally {setPending(false);}
  }
  async function rename(event:FormEvent) {
    event.preventDefault();
    setPending(true);
    try {
      await call(`/services/${service}`,session.token,{name:draft},"PATCH");
      await cache.invalidateQueries({queryKey:["services"]});
      setResponse("Service name updated.");
    } catch (failure) {
      setResponse(failure instanceof Error ? failure.message : "Service update failed.");
    } finally {setPending(false);}
  }
  return <div className="inline-controls">
    <button onClick={()=>void testConnection()} disabled={pending}>{pending ? "Checking…" : "Test connection"}</button>
    <Link to={`/crawler?service=${service}`}>Open crawler</Link>
    <details><summary>Edit service name</summary><form className="form-panel compact" onSubmit={rename}>
      <label>New display name<input value={draft} onChange={(event)=>setDraft(event.target.value)} maxLength={100} required /></label>
      <button disabled={pending||!draft.trim()||draft===name}>Save service name</button>
    </form></details>
    {response && <p role="status">{response}</p>}
  </div>;
}

export function Crawler({session}:{session:Session}) {
  const [parameters,setParameters]=useSearchParams();
  const service=parameters.get("service") ?? "";
  const job=parameters.get("job");
  const [error,setError]=useState("");
  const [pending,setPending]=useState(false);
  const cache=useQueryClient();
  const services=useQuery({queryKey:["services",session.id],queryFn:()=>call<ServiceOption[]>("/services",session.token), enabled:session.id==="platform-admin"});
  const crawl=useQuery({queryKey:["crawl-jobs",session.id],queryFn:()=>call<CrawlJob[]>("/crawl-jobs",session.token),retry:false,refetchInterval:2000,enabled:session.id==="platform-admin"});
  const records=crawl.data?.filter((item)=>!service||item.service_id===service) ?? [];
  const selected=crawl.data?.find((item)=>item.id===job);
  async function enqueue(id:string) {
    setPending(true);
    setError("");
    try {
      const result=await call<{id:string}>(`/services/${id}/crawl`,session.token,{});
      await cache.invalidateQueries({queryKey:["crawl-jobs"]});
      setParameters({service:id,job:result.id});
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not queue crawl.");
    } finally {setPending(false);}
  }
  if (session.id!=="platform-admin") return <Screen name="AutoCrawler" description="Only the platform administrator can inspect or run crawl jobs." />;
  return <Screen name="AutoCrawler" description="Inspect source metadata, stage drafts, and see incompatible releases quarantined.">
    <State loading={services.isPending} error={services.error} retry={services.refetch}>
      <div className="inline-controls"><label>Service <select value={service} onChange={(event)=>setParameters({service:event.target.value})}>
        <option value="">All registered services</option>{services.data?.map((option)=><option key={option.id} value={option.id}>{option.name}</option>)}
      </select></label><button disabled={pending||!service} onClick={()=>void enqueue(service)}>Run crawl</button></div>
    </State>
    {error && <p role="alert" className="warning">{error}</p>}
    <RecordWorkspace selected={selected?.id ?? null}>
    <div className="record-list"><State loading={crawl.isPending} error={crawl.error} retry={crawl.refetch}>
      {records.length===0 ? <p>No crawl jobs in this view.</p> : <div className="panel table-wrap"><table>
        <thead><tr><th scope="col">Service</th><th scope="col">Job</th><th scope="col">Status</th></tr></thead>
        <tbody>{records.map((record)=><tr key={record.id} data-selected={record.id === selected?.id}><th scope="row">{record.service_id}</th>
          <td><Link to={`?service=${service}&job=${record.id}`}>{record.id.slice(0,8)}</Link></td><td><StatusLabel value={record.status} /></td></tr>)}</tbody>
      </table></div>}
    </State></div>
    {selected && <section className="panel detail" aria-label="Crawl job">
      <h2>{selected.service_id} crawl</h2>
      <p role="status">{selected.status}{selected.error&&` · ${selected.error}`}</p>
      <p>Requested by {selected.actor} · {new Date(selected.requested_at).toLocaleString()}</p>
      {selected.finished_at && <p>Finished {new Date(selected.finished_at).toLocaleString()}</p>}
      {selected.status==="FAILED" && <button disabled={pending} onClick={()=>void enqueue(selected.service_id)}>Retry crawl</button>}
      {selected.changes.map((change,index)=><section key={`${change.tool_id??change.name}-${index}`}>
        <p>{change.tool_id&&change.version
          ? <Link to={`/tools?service=${selected.service_id}&tool=${change.tool_id}&version=${change.version}`}>{change.tool_id} v{change.version}</Link>
          : change.tool_id??change.name} · {change.status}{change.reason&&` · ${change.reason}`}</p>
        {change.diff && Object.keys(change.diff).length>0 && <details><summary>Schema diff</summary><pre tabIndex={0} role="region" aria-label="Schema diff code">{JSON.stringify(change.diff,null,2)}</pre></details>}
      </section>)}
    </section>}
    </RecordWorkspace>
  </Screen>;
}
