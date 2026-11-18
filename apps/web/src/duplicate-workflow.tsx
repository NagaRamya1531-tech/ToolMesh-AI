import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { call } from "./api";
import { Screen, State, type Session } from "./workspace";

type Workflow = {
  id: string; order_id: string; status: string; refund_status: string | null;
  ticket_id: string | null; refund_id: string | null; notification_id: string | null;
  evidence: { groups: string[][]; trace_id: string } | null;
};

export function DuplicateWorkflow({ session }: { session: Session }) {
  const [params, setParams] = useSearchParams();
  const [order, setOrder] = useState("O-102");
  const [requestId, setRequestId] = useState(() => crypto.randomUUID());
  const [transaction, setTransaction] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["duplicate-workflows", session.id],
    queryFn: () => call<Workflow[]>("/workflows", session.token),
    refetchInterval: 2500, retry: false,
  });
  const run = query.data?.find((entry) => entry.id === params.get("run"));
  const support = session.id === "support-agent";
  const finance = session.id === "finance-agent";
  const reviewer = session.id === "platform-admin" || session.id === "refund-approver";

  async function action(path: string, body: object) {
    setPending(true);
    setError("");
    try {
      const updated = await call<Workflow>(path, session.token, body);
      await client.invalidateQueries({ queryKey: ["duplicate-workflows"] });
      setParams({ run: updated.id });
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Workflow request failed.");
    } finally {
      setPending(false);
    }
  }

  async function start(event: FormEvent) {
    event.preventDefault();
    await action("/workflows", { id: requestId, order_id: order });
  }

  return <Screen name="Duplicate payment workflow" description="Investigate a capture, hand off the refund to Finance and return to Support after confirmation.">
    {support && <form className="panel form-panel" onSubmit={start}>
      <label>Order ID<input value={order} required maxLength={64} onChange={(event) => {setOrder(event.target.value); setRequestId(crypto.randomUUID());}} /></label>
      <button disabled={pending}>Investigate order</button>
    </form>}
    {error && <p className="warning" role="alert">{error}</p>}
    <State loading={query.isPending} error={query.error} retry={query.refetch}>
      {query.data?.length === 0 ? <p>No saved workflow runs.</p> : <section className="panel table-wrap detail">
        <table><thead><tr><th scope="col">Order</th><th scope="col">Workflow status</th><th scope="col">Refund status</th></tr></thead>
          <tbody>{query.data?.map((entry) => <tr key={entry.id}>
            <th scope="row"><Link to={`?run=${entry.id}`} onClick={() => {setTransaction("");setError("");}}>{entry.order_id} · {entry.id.slice(0,8)}</Link></th>
            <td>{entry.status}</td><td>{entry.refund_status ?? "—"}</td>
          </tr>)}</tbody>
        </table>
      </section>}
    </State>
    {run && <section className="panel detail" aria-label="Workflow record">
      <h2>{run.order_id} investigation</h2>
      <ol>
        <li>Duplicate evidence {run.evidence
          ? <>{(support || reviewer) && <Link to={`/executions?trace=${run.evidence.trace_id}`}>Inspect check</Link>}
              <p>{run.evidence.groups.length ? run.evidence.groups.flat().join(", ") : "No duplicate capture found."}</p></>
          : <p>Waiting for a completed duplicate check.</p>}</li>
        {run.ticket_id && <li>Support ticket <code>{run.ticket_id}</code></li>}
        {run.refund_id && <li>{finance || reviewer ? <Link to={`/approvals?approval=${run.refund_id}`}>Refund approval</Link> : "Refund approval"} · {run.refund_status}</li>}
        {run.notification_id && <li>Simulated notification <code>{run.notification_id}</code></li>}
        {run.status === "RESOLVED" && <li>Ticket resolved</li>}
      </ol>
      {finance && run.status === "TICKET_OPEN" && <div className="form-panel compact">
        <label>Duplicate capture to refund<select value={transaction} onChange={(event) => setTransaction(event.target.value)}>
          <option value="">Choose a capture</option>{run.evidence?.groups.flat().map((id) => <option key={id}>{id}</option>)}
        </select></label>
        <button disabled={pending || !transaction} onClick={() => void action(`/workflows/${run.id}/refund`, { transaction_id: transaction })}>Request refund</button>
      </div>}
      {support && run.refund_status === "SUCCEEDED" && run.status !== "RESOLVED" &&
        <button disabled={pending} onClick={() => void action(`/workflows/${run.id}/complete`, {})}>Notify customer and resolve ticket</button>}
    </section>}
  </Screen>;
}
