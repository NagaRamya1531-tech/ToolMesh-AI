import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { call } from "./api";
import { Screen, State, type Session } from "./workspace";

type Approval = {
  id: string; requester: string; customer_id: string; transaction_id: string|null; service_id: string|null;
  tool_id: string; version: number; currency: string|null; amount_minor: number|null;
  status: string; execution_status: string; expires_at: string; decided_by: string | null;
  decision_reason: string | null;
};
type AuditEvent = { id: number; actor: string; action: string; resource_id: string; created_at: string };
type OverviewData = {
  hours: number; pending: number; outcomes: { status: string; count: number }[];
  services: { id: string; name: string; published_tools: number }[];
};
const approvalStatuses = ["PENDING", "APPROVED", "REJECTED", "EXPIRED"];

export function Approvals({ session }: { session: Session }) {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const selected = params.get("approval");
  const [reason, setReason] = useState("");
  const [feedback, setFeedback] = useState<{ text: string; error: boolean } | null>(null);
  const [pending, setPending] = useState(false);
  const cache = useQueryClient();
  const query = useQuery({
    queryKey: ["approvals", session.id], queryFn: () => call<Approval[]>("/approvals", session.token),
    refetchInterval: 2500,
  });
  const approvals = query.data?.filter((approval) => !status || approval.status === status) ?? [];
  const current = query.data?.find((approval) => approval.id === selected);
  async function decide(action: "approve" | "reject") {
    if (!current || !reason.trim()) return;
    setFeedback(null);
    setPending(true);
    try {
      const result = await call<{ status: string; error_code: string | null }>(`/approvals/${current.id}/${action}`, session.token, { reason });
      setFeedback({ text: `Execution ${result.status}${result.error_code ? ` · ${result.error_code}` : ""}`, error: false });
      setReason("");
    } catch (failure) {
      setFeedback({ text: failure instanceof Error ? failure.message : "Approval decision failed.", error: true });
    } finally {
      await Promise.all(["approvals", "executions", "audit", "overview"].map((key) => cache.invalidateQueries({ queryKey: [key] })));
      setPending(false);
    }
  }
  return <Screen name="Approvals" description="Review the exact intent bound to a sandbox refund or simulated restart.">
    <label className="filter">Status <select value={status} onChange={(event) => setParams(event.target.value ? { status: event.target.value } : {})}>
      <option value="">All statuses</option>{approvalStatuses.map((value) => <option key={value}>{value}</option>)}
    </select></label>
    <State loading={query.isPending} error={query.error} retry={query.refetch}>
      {approvals.length === 0 ? <p>No refund approvals in this view.</p> : <div className="panel table-wrap"><table>
        <thead><tr><th scope="col">Transaction</th><th scope="col">Amount (minor units)</th><th scope="col">Approval</th><th scope="col">Execution</th></tr></thead>
        <tbody>{approvals.map((approval) => <tr key={approval.id}>
          <th scope="row"><Link to={`?status=${status}&approval=${approval.id}`} onClick={() => {setReason(""); setFeedback(null);}}>{approval.transaction_id ?? approval.service_id}</Link></th>
          <td>{approval.amount_minor?.toLocaleString() ?? "—"} {approval.currency}</td><td>{approval.status}</td><td>{approval.execution_status}</td>
        </tr>)}</tbody>
      </table></div>}
    </State>
    {current && <section className="panel detail" aria-label={current.service_id ? "Restart intent" : "Refund intent"}>
      <h2>{current.service_id ? "Restart intent" : "Refund intent"}</h2>
      <dl>
        <dt>Requester</dt><dd>{current.requester}</dd><dt>Customer</dt><dd>{current.customer_id}</dd>
        {current.service_id ? <><dt>Service</dt><dd>{current.service_id}</dd></> :
          <><dt>Transaction</dt><dd>{current.transaction_id}</dd><dt>Amount (minor units)</dt><dd>{current.amount_minor?.toLocaleString()} {current.currency}</dd></>}
        <dt>Tool version</dt><dd>{current.tool_id} v{current.version}</dd>
        <dt>Expires</dt><dd>{new Date(current.expires_at).toLocaleString()}</dd>
        <dt>Approval</dt><dd>{current.status}</dd><dt>Execution</dt><dd>{current.execution_status}</dd>
        {current.decided_by && <><dt>Decided by</dt><dd>{current.decided_by}</dd><dt>Reason</dt><dd>{current.decision_reason}</dd></>}
      </dl>
      <Link to={`/executions?trace=${current.id}`}>View execution trace</Link>
      {current.status === "PENDING" && session.id === "refund-approver" && <div className="form-panel compact">
        <label>Decision reason<input required value={reason} maxLength={240} disabled={pending} onChange={(event) => setReason(event.target.value)} /></label>
        <div className="inline-controls">
          <button disabled={pending || !reason.trim()} onClick={() => void decide("approve")}>{current.service_id?"Approve restart":"Approve refund"}</button>
          <button disabled={pending || !reason.trim()} onClick={() => void decide("reject")}>{current.service_id?"Reject restart":"Reject refund"}</button>
        </div>
      </div>}
      {feedback && <p role={feedback.error ? "alert" : "status"}>{feedback.text}</p>}
    </section>}
  </Screen>;
}

export function Audit({ session }: { session: Session }) {
  const [params, setParams] = useSearchParams();
  const action = params.get("action") ?? "";
  const actor = params.get("actor") ?? "";
  const query = useQuery({ queryKey: ["audit", session.id], queryFn: () => call<AuditEvent[]>("/audit-events", session.token), retry: false });
  const all = query.data ?? [];
  const events = all.filter((event) => (!action || event.action === action) && (!actor || event.actor === actor));
  function exportCsv() {
    const records = [["created_at", "actor", "action", "resource_id"],
      ...events.map((event) => [event.created_at, event.actor, event.action, event.resource_id])];
    const csv = records.map((record) => record.map((value) => `"${value.replaceAll('"', '""')}"`).join(",")).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "toolmesh-audit.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <Screen name="Audit" description="Review append-only refund approval and execution decisions.">
    <div className="inline-controls"><label>Action <select value={action} onChange={(event) => setParams({ action: event.target.value, actor })}>
      <option value="">All actions</option>{[...new Set(all.map((event) => event.action))].map((value) => <option key={value}>{value}</option>)}
    </select></label>
    <label>Actor <select value={actor} onChange={(event) => setParams({ action, actor: event.target.value })}>
      <option value="">All actors</option>{[...new Set(all.map((event) => event.actor))].map((value) => <option key={value}>{value}</option>)}
    </select></label>
    <button disabled={!events.length} onClick={exportCsv}>Export CSV</button></div>
    <State loading={query.isPending} error={query.error} retry={query.refetch}>
      {events.length === 0 ? <p>No audit events in this view.</p> : <div className="panel table-wrap"><table>
        <thead><tr><th scope="col">Time</th><th scope="col">Actor</th><th scope="col">Action</th><th scope="col">Evidence</th></tr></thead>
        <tbody>{events.map((event) => <tr key={event.id}>
          <th scope="row">{new Date(event.created_at).toLocaleString()}</th><td>{event.actor}</td>
          <td>{event.action}</td><td>{(event.action.startsWith("REFUND_")||event.action.startsWith("RESTART_"))
            ? <Link to={`/approvals?approval=${event.resource_id}`}>Approval intent</Link>
            : event.action.startsWith("POLICY_") ? <Link to={`/policies?rule=${event.resource_id}`}>Policy history</Link>
            : event.action==="QUOTAS_UPDATED" ? <Link to="/settings">Quota settings</Link>
            : <Link to={`/executions?trace=${event.resource_id}`}>Execution trace</Link>}</td>
        </tr>)}</tbody>
      </table></div>}
    </State>
  </Screen>;
}

export function OverviewActivity({ session }: { session: Session }) {
  const [params, setParams] = useSearchParams();
  const hours = ["1", "24", "168"].includes(params.get("hours") ?? "") ? params.get("hours")! : "24";
  const query = useQuery({ queryKey: ["overview", session.id, hours], queryFn: () => call<OverviewData>(`/overview?hours=${hours}`, session.token), refetchInterval: 15000 });
  const total = query.data?.outcomes.reduce((sum, entry) => sum + entry.count, 0) ?? 0;
  return <section className="panel activity-panel" aria-labelledby="activity-heading">
    <header className="panel-heading">
      <h2 id="activity-heading">Execution activity</h2>
      <label className="activity-filter">Time range <select value={hours} onChange={(event) => setParams({ hours: event.target.value })}>
        <option value="1">Last hour</option><option value="24">Last 24 hours</option><option value="168">Last 7 days</option>
      </select></label>
    </header>
    <div className="activity-content">
      <State loading={query.isPending} error={query.error} retry={query.refetch}>
        {query.data && <>
          <div className="activity-metrics">
            <Link to={`/executions?hours=${hours}`}><strong>{total.toLocaleString()}</strong><span>Executions in range</span></Link>
            <Link to="/approvals?status=PENDING"><strong>{query.data.pending.toLocaleString()}</strong><span>Pending approvals</span></Link>
          </div>
          {total ? <ul className="outcomes activity-outcomes">{query.data.outcomes.map((item) => <li key={item.status} data-outcome={item.status}>
            <Link to={`/executions?hours=${hours}&status=${item.status}`}>{item.status.replaceAll("_", " ")}</Link>
            <meter min={0} max={total} value={item.count} aria-label={`${item.status} executions`} />
            <span>{item.count.toLocaleString()}</span>
          </li>)}</ul> : <p className="activity-empty">No executions in this time range.</p>}
        </>}
      </State>
    </div>
    {query.data && <footer>Last refreshed <time dateTime={new Date(query.dataUpdatedAt).toISOString()}>{new Date(query.dataUpdatedAt).toLocaleTimeString()}</time></footer>}
  </section>;
}
