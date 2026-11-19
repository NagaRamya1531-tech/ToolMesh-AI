import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { call } from "./api";
import { Screen, State } from "./screen";
import type { Session } from "./workspace";

const profiles = ["support","finance","it","hr"];
type Run = {
  id:string; profile:string; status:string; approval_id:string|null; created_at:string;
  steps:{tool:string;id:string|null;status:string;error_code:string|null;summary:Record<string,unknown>}[];
};
const scenarios:Record<string,string>={
  support:"Check the customer, their persisted transactions, and duplicate captures.",
  finance:"Compare actual sandbox sales and refunds, then persist a date-bounded report.",
  it:"Inspect service health and logs, record an incident, and wait for a restart decision.",
  hr:"Check the scoped allowance, reserve leave, and record a simulated notification.",
};

export function AgentScenarios({session}:{session:Session}) {
  const [params,setParams]=useSearchParams();
  const selected=params.get("run");
  const [pending,setPending]=useState(false);
  const [error,setError]=useState("");
  const [requestId,setRequestId]=useState(()=>crypto.randomUUID());
  const cache=useQueryClient();
  const supported=profiles.includes(session.profile);
  const query=useQuery({
    queryKey:["agent-runs",session.id],queryFn:()=>call<Run[]>("/playground/runs",session.token),
    enabled:supported,refetchInterval:1500,retry:false,
  });
  const active=query.data?.find((item)=>item.id===selected);
  async function perform(path:string,body:object) {
    setError("");setPending(true);
    try {
      const run=await call<Run>(path,session.token,body);
      setParams({run:run.id});
      if (path==="/playground/runs") setRequestId(crypto.randomUUID());
      await cache.invalidateQueries({queryKey:["agent-runs"]});
    } catch (failure) {setError(failure instanceof Error?failure.message:"Scenario request failed.");}
    finally {setPending(false);}
  }
  function start(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const args: Record<string,string> = {};
    new FormData(event.currentTarget).forEach((value,key)=>{ if (typeof value==="string") args[key]=value; });
    void perform("/playground/runs",{request_id:requestId,arguments:args});
  }
  return <Screen name="Agent scenarios" description="Scripted LangGraph runs execute published tools under the signed-in profile.">
    {supported ? <>
      <form className="panel form-panel" onSubmit={start} onChange={()=>setRequestId(crypto.randomUUID())}>
        <h2>{session.profile} scenario</h2><p>{scenarios[session.profile]}</p>
        {session.profile==="support" && <label>Order ID<input name="order_id" defaultValue="O-102" required /></label>}
        {session.profile==="it" && <label>Service ID<input name="service_id" defaultValue="sandbox-api" required /></label>}
        {session.profile==="hr" && <label>Employee ID<input name="employee_id" defaultValue="E-102" required /></label>}
        {["finance","hr"].includes(session.profile) && <>
          <label>Start date<input type="date" name="start_date" defaultValue={session.profile==="finance"?"2026-11-01":undefined} required /></label>
          <label>End date<input type="date" name="end_date" defaultValue={session.profile==="finance"?"2026-11-30":undefined} required /></label>
        </>}
        {session.profile==="finance" && <>
          <label>Previous start date<input type="date" name="previous_start_date" defaultValue="2026-10-01" required /></label>
          <label>Previous end date<input type="date" name="previous_end_date" defaultValue="2026-10-31" required /></label>
        </>}
        <button disabled={pending}>Run scenario</button>
      </form>
      {error && <p role="alert" className="warning">{error}</p>}
      <State loading={query.isPending} error={query.error} retry={query.refetch}>
        {query.data?.length ? <div className="panel table-wrap detail"><table>
          <thead><tr><th scope="col">Run</th><th scope="col">Profile</th><th scope="col">Status</th></tr></thead>
          <tbody>{query.data.map((item)=><tr key={item.id}>
            <th scope="row"><Link to={`?run=${item.id}`}>{item.id.slice(0,8)}</Link></th><td>{item.profile}</td><td>{item.status}</td>
          </tr>)}</tbody>
        </table></div>:<p>No saved scenario runs.</p>}
      </State>
      {active && <section className="panel detail" aria-label="Scenario record">
        <h2>Ordered tool steps</h2>
        <p role="status">{active.status}</p>
        <ol>{active.steps.map((step,index)=><li key={`${index}-${step.tool}`}>
          <p>{step.tool} · {step.status}{step.error_code&&` · ${step.error_code}`}</p>
          {step.id&&<Link to={`/executions?trace=${step.id}`}>View execution</Link>}
          {!!Object.keys(step.summary).length&&<pre>{JSON.stringify(step.summary,null,2)}</pre>}
        </li>)}</ol>
        {active.status==="WAITING_FOR_APPROVAL" && <div className="inline-controls">
          <Link to={`/approvals?approval=${active.approval_id}`}>Review approval status</Link>
          <button disabled={pending} onClick={()=>void perform(`/playground/runs/${active.id}/resume`,{})}>Resume after decision</button>
        </div>}
        {active.profile==="support"&&active.status==="SUCCEEDED"&&<Link to="/playground/duplicate-payment">Open the duplicate-payment handoff workflow</Link>}
      </section>}
    </>:<p>Sign in with an assigned Support, Finance, IT, or HR profile to run a scenario.</p>}
  </Screen>;
}
