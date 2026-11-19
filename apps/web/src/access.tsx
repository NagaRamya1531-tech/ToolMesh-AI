import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { call } from "./api";
import { Screen, State, StatusLabel } from "./screen";
import type { Session } from "./workspace";

type Agent = {id:string;profile:string;enabled:boolean;origin:string};
type Rule = {id:string;principal:string;tool_id:string;customer_id:string;effect:string;enabled:boolean;version:number};
type Revision = {version:number;actor:string;reason:string;enabled:boolean;changed_at:string};
type Tool = {id:string};
type Quotas = {per_tool:number;global_limit:number};
const errorText = (failure:unknown) => failure instanceof Error ? failure.message : "The request failed.";

export function Agents({session}:{session:Session}) {
  const allowed=session.id==="platform-admin";
  const query=useQuery({queryKey:["agents",session.id],queryFn:()=>call<Agent[]>("/agents",session.token),enabled:allowed,retry:false});
  const [credential,setCredential]=useState<{id:string;credential:string}|null>(null);
  const [pending,setPending]=useState(false);
  const [error,setError]=useState("");
  const cache=useQueryClient();
  if (!allowed) return <Screen name="Agents" description="Agent administration requires the platform administrator." />;
  async function create(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form=new FormData(event.currentTarget);
    setError("");setPending(true);setCredential(null);
    try {
      const result=await call<{id:string;credential:string}>("/agents",session.token,{id:form.get("id"),profile:form.get("profile")});
      setCredential(result);
      await cache.invalidateQueries({queryKey:["agents"]});
    } catch (failure) {setError(errorText(failure));}
    finally {setPending(false);}
  }
  async function toggle(agent:Agent) {
    setError("");setPending(true);
    try {
      await call(`/agents/${agent.id}`,session.token,{enabled:!agent.enabled},"PATCH");
      await cache.invalidateQueries({queryKey:["agents"]});
    } catch (failure) {setError(errorText(failure));}
    finally {setPending(false);}
  }
  return <Screen name="Agents" description="Create scoped sandbox identities and revoke their active sessions.">
    <div className="workspace-tools-grid">
    <div className="workspace-tool">
    <form className="panel form-panel workspace-setup" onSubmit={create}>
      <h2>New agent</h2>
      <label>Agent ID<input name="id" pattern="[a-z][a-z0-9\-]{2,47}" required /></label>
      <label>Profile<select name="profile"><option value="support">Support</option><option value="finance">Finance</option><option value="it">IT</option><option value="hr">HR</option></select></label>
      <button disabled={pending}>Create agent</button>
    </form>
    {error && <p role="alert" className="warning">{error}</p>}
    {credential && <section className="panel detail" aria-label="One-time agent credential">
      <h2>Credential for {credential.id}</h2>
      <p>Copy it now. The server stores a hash and will never display this value again.</p>
      <p><code>{credential.credential}</code></p>
      <button onClick={()=>setCredential(null)}>Dismiss credential</button>
    </section>}
    </div>
    <div className="workspace-tool"><State loading={query.isPending} error={query.error} retry={query.refetch}>
      <div className="panel table-wrap detail"><table>
        <thead><tr><th scope="col">Agent</th><th scope="col">Profile</th><th scope="col">Credential source</th><th scope="col">Status</th><th scope="col">Action</th></tr></thead>
        <tbody>{query.data?.map((agent)=><tr key={agent.id}>
          <th scope="row"><Link to={`/policies?principal=${agent.id}`}>{agent.id}</Link></th>
          <td>{agent.profile}</td><td>{agent.origin}</td><td><StatusLabel value={agent.enabled ? "Enabled" : "Disabled"} /></td>
          <td><button disabled={pending} onClick={()=>void toggle(agent)} aria-label={`${agent.enabled?"Disable":"Enable"} ${agent.id}`}>{agent.enabled?"Disable":"Enable"}</button></td>
        </tr>)}</tbody>
      </table></div>
    </State></div>
    </div>
  </Screen>;
}

export function Policies({session}:{session:Session}) {
  const allowed=session.id==="platform-admin";
  const [params,setParams]=useSearchParams();
  const principal=params.get("principal")??"";
  const selected=params.get("rule");
  const [result,setResult]=useState<{allowed:boolean;reason:string}|null>(null);
  const [pending,setPending]=useState(false);
  const [feedback,setFeedback]=useState("");
  const cache=useQueryClient();
  const rules=useQuery({queryKey:["policies",session.id],queryFn:()=>call<Rule[]>("/policies",session.token),enabled:allowed,retry:false});
  const agents=useQuery({queryKey:["agents",session.id],queryFn:()=>call<Agent[]>("/agents",session.token),enabled:allowed,retry:false});
  const tools=useQuery({queryKey:["policy-tools",session.id],queryFn:()=>call<Tool[]>("/tools",session.token),enabled:allowed,retry:false});
  const history=useQuery({queryKey:["policy-history",session.id,selected],queryFn:()=>call<Revision[]>(`/policies/${selected}/history`,session.token),enabled:allowed&&!!selected,retry:false});
  if (!allowed) return <Screen name="Policies" description="Policy editing requires the platform administrator." />;
  const current=rules.data?.find((rule)=>rule.id===selected);
  const toolIds=[...new Set(tools.data?.map((tool)=>tool.id))];
  async function act(event:FormEvent<HTMLFormElement>,mode:"create"|"update"|"simulate") {
    event.preventDefault();setPending(true);setFeedback("");setResult(null);
    const form=new FormData(event.currentTarget);
    try {
      if (mode==="create") {
        const record=await call<{id:string}>("/policies",session.token,{principal:form.get("principal"),tool_id:form.get("tool"),effect:form.get("effect"),reason:form.get("reason")});
        setParams({principal:String(form.get("principal")),rule:record.id});
        setFeedback("Policy created.");
      } else if (mode==="update" && current) {
        await call(`/policies/${current.id}`,session.token,{enabled:!current.enabled,version:current.version,reason:form.get("reason")},"PATCH");
        setFeedback("Policy revision recorded.");
      } else if (mode==="simulate") {
        const args:unknown=JSON.parse(String(form.get("args")));
        if (!args || typeof args!=="object" || Array.isArray(args)) throw new Error("Use a JSON object for arguments.");
        setResult(await call("/policies/simulate",session.token,{principal:form.get("principal"),tool_id:form.get("tool"),arguments:args}));
      }
      if (mode!=="simulate") await Promise.all(["policies","policy-history"].map((key)=>cache.invalidateQueries({queryKey:[key]})));
    } catch (failure) {setFeedback(errorText(failure));}
    finally {setPending(false);}
  }
  const selectors = <>
    <label>Principal<select name="principal" defaultValue={principal} required>
      <option value="">Choose an agent</option>{agents.data?.filter((agent)=>agent.enabled).map((agent)=><option key={agent.id}>{agent.id}</option>)}
    </select></label>
    <label>Tool<select name="tool" required><option value="">Choose a tool</option>{toolIds.map((id)=><option key={id}>{id}</option>)}</select></label>
  </>;
  return <Screen name="Policies" description="Manage scoped allow and deny rules. A deny takes precedence over an allow.">
    <State loading={agents.isPending||tools.isPending} error={agents.error??tools.error} retry={()=>{void agents.refetch();void tools.refetch();}}>
      <div className="governance-grid">
        <form className="panel form-panel" onSubmit={(event)=>void act(event,"create")}>
          <h2>Create rule</h2>{selectors}
          <label>Effect<select name="effect"><option value="deny">Deny</option><option value="allow">Allow</option></select></label>
          <label>Reason<input name="reason" maxLength={240} required /></label>
          <button disabled={pending}>Create policy</button>
        </form>
        <form className="panel form-panel" onSubmit={(event)=>void act(event,"simulate")}>
          <h2>Simulate</h2>{selectors}
          <label>Arguments<textarea name="args" defaultValue={'{"customer_id":"C-102"}'} required /></label>
          <button disabled={pending}>Check access</button>
          {result && <p role="status">{result.allowed?"Allowed":"Denied"} · {result.reason}</p>}
        </form>
      </div>
    </State>
    {feedback && <p role="status">{feedback}</p>}
    <label className="filter">Filter principal<select value={principal} onChange={(event)=>setParams(event.target.value?{principal:event.target.value}:{})}>
      <option value="">All principals</option>{agents.data?.map((agent)=><option key={agent.id}>{agent.id}</option>)}
    </select></label>
    <State loading={rules.isPending} error={rules.error} retry={rules.refetch}>
      {rules.data?.length ? <div className="panel table-wrap"><table>
        <thead><tr><th scope="col">Tool</th><th scope="col">Principal</th><th scope="col">Effect</th><th scope="col">State</th></tr></thead>
        <tbody>{rules.data.filter((rule)=>!principal||rule.principal===principal).map((rule)=><tr key={rule.id}>
          <th scope="row"><Link to={`?principal=${principal}&rule=${rule.id}`}>{rule.tool_id}</Link></th>
          <td>{rule.principal}</td><td><StatusLabel value={rule.effect} /></td><td><StatusLabel value={rule.enabled ? "Enabled" : "Disabled"} /></td>
        </tr>)}</tbody>
      </table></div>:<p>No managed policies.</p>}
    </State>
    {current && <section className="panel detail" aria-label="Policy history">
      <h2>{current.tool_id} · v{current.version}</h2>
      <form className="form-panel compact" onSubmit={(event)=>void act(event,"update")}>
        <label>Revision reason<input name="reason" maxLength={240} required /></label>
        <button disabled={pending}>{current.enabled?"Disable rule":"Enable rule"}</button>
      </form>
      <State loading={history.isPending} error={history.error} retry={history.refetch}>
        <ul>{history.data?.map((revision)=><li key={revision.version}>v{revision.version} · {revision.actor} · {revision.enabled?"enabled":"disabled"} · {revision.reason}</li>)}</ul>
      </State>
    </section>}
  </Screen>;
}

export function Settings({session}:{session:Session}) {
  const allowed=session.id==="platform-admin";
  const query=useQuery({queryKey:["quotas",session.id],queryFn:()=>call<Quotas>("/settings/quotas",session.token),enabled:allowed,retry:false});
  const [pending,setPending]=useState(false);
  const [message,setMessage]=useState("");
  if (!allowed) return <Screen name="Settings" description="Quota settings require the platform administrator." />;
  async function submit(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form=new FormData(event.currentTarget);
    setMessage("");setPending(true);
    try {
      const data=await call<Quotas>("/settings/quotas",session.token,{per_tool:Number(form.get("tool")),global_limit:Number(form.get("global"))},"PATCH");
      await query.refetch();
      setMessage(`Saved ${data.per_tool} per tool and ${data.global_limit} globally per minute.`);
    } catch (failure) {setMessage(errorText(failure));}
    finally {setPending(false);}
  }
  return <Screen name="Settings" description="Configure per-principal tool and global execution quotas over fixed one-minute windows.">
    <State loading={query.isPending} error={query.error} retry={query.refetch}>
      {query.data && <form className="panel form-panel workspace-setup" onSubmit={submit}>
        <h2>Execution quotas</h2>
        <label>Per-principal tool quota<input name="tool" type="number" min={1} max={600} defaultValue={query.data.per_tool} required /></label>
        <label>Global execution quota<input name="global" type="number" min={1} max={10000} defaultValue={query.data.global_limit} required /></label>
        <button disabled={pending}>Save quotas</button>
        {message && <p role="status">{message}</p>}
      </form>}
    </State>
  </Screen>;
}
