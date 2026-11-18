import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { call } from "./api";
import { Screen, State, type Session } from "./workspace";

type Approval = {
  id: string; requester: string; customer_id: string; transaction_id: string;
  tool_id: string; version: number; currency: string; amount_minor: number;
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
  return <Screen name="Approvals" description="Review the exact amount, currency and transaction bound to each sandbox refund request.">
    <label className="filter">Status <select value={status} onChange={(event) => setParams(event.target.value ? { status: event.target.value } : {})}>
      <option value="">All statuses</option>{approvalStatuses.map((value) => <option key={value}>{value}</option>)}
    </select></label>
    <State loading={query.isPending} error={query.error} retry={query.refetch}>
      {approvals.length === 0 ? <p>No refund approvals in this view.</p> : <div className="panel table-wrap"><table>
        <thead><tr><th scope="col">Transaction</th><th scope="col">Amount (minor units)</th><th scope="col">Approval</th><th scope="col">Execution</th></tr></thead>
        <tbody>{approvals.map((approval) => <tr key={approval.id}>
          <th scope="row"><Link to={`?status=${status}&approval=${approval.id}`} onClick={() => {setReason(""); setFeedback(null);}}>{approval.transaction_id}</Link></th>
          <td>{approval.amount_minor.toLocaleString()} {approval.currency}</td><td>{approval.status}</td><td>{approval.execution_status}</td>
        </tr>)}</tbody>
      </table></div>}
    </State>
    {current && <section className="panel detail" aria-label="Refund intent">
      <h2>Refund intent</h2>
      <dl>
        <dt>Requester</dt><dd>{current.requester}</dd><dt>Customer</dt><dd>{current.customer_id}</dd>
        <dt>Transaction</dt><dd>{current.transaction_id}</dd><dt>Amount (minor units)</dt><dd>{current.amount_minor.toLocaleString()} {current.currency}</dd>
        <dt>Tool version</dt><dd>{current.tool_id} v{current.version}</dd>
        <dt>Expires</dt><dd>{new Date(current.expires_at).toLocaleString()}</dd>
        <dt>Approval</dt><dd>{current.status}</dd><dt>Execution</dt><dd>{current.execution_status}</dd>
        {current.decided_by && <><dt>Decided by</dt><dd>{current.decided_by}</dd><dt>Reason</dt><dd>{current.decision_reason}</dd></>}
      </dl>
      <Link to={`/executions?trace=${current.id}`}>View execution trace</Link>
      {current.status === "PENDING" && session.id === "refund-approver" && <div className="form-panel compact">
        <label>Decision reason<input required value={reason} maxLength={240} disabled={pending} onChange={(event) => setReason(event.target.value)} /></label>
        <div className="inline-controls">
          <button disabled={pending || !reason.trim()} onClick={() => void decide("approve")}>Approve refund</button>
          <button disabled={pending || !reason.trim()} onClick={() => void decide("reject")}>Reject refund</button>
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
          <td>{event.action}</td><td>{event.action.startsWith("REFUND_")
            ? <Link to={`/approvals?approval=${event.resource_id}`}>Refund intent</Link>
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
  return <section className="panel detail" aria-label="Execution activity">
    <div className="inline-controls"><h2>Execution activity</h2><label>Time range <select value={hours} onChange={(event) => setParams({ hours: event.target.value })}>
      <option value="1">Last hour</option><option value="24">Last 24 hours</option><option value="168">Last 7 days</option>
    </select></label></div>
    <State loading={query.isPending} error={query.error} retry={query.refetch}>
      {query.data && <>
        <p><Link to={`/executions?hours=${hours}`}>{total} executions</Link> · <Link to="/approvals?status=PENDING">{query.data.pending} currently pending approvals</Link></p>
        {total ? <ul className="outcomes">{query.data.outcomes.map((item) => <li key={item.status}>
          <Link to={`/executions?hours=${hours}&status=${item.status}`}>{item.status} · {item.count}</Link><meter min={0} max={total} value={item.count} aria-label={`${item.status} executions`} />
        </li>)}</ul> : <p>No executions in this time range.</p>}
        <p className="breadcrumb">Last refreshed {new Date(query.dataUpdatedAt).toLocaleTimeString()}</p>
      </>}
    </State>
  </section>;
}
