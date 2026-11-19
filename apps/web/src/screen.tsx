import { useEffect, useRef, type ReactNode } from "react";
import { consoleSection } from "./navigation";

export function Screen({ name, description, children }: { name: string; description: string; children?: ReactNode }) {
  const section = consoleSection(name);
  return <main className="workspace-page" data-section={section?.tone} id="main" tabIndex={-1}>
    <header className="workspace-page-header">
      <p className="breadcrumb">{section?.label ?? "Workspace"} <span aria-hidden="true">/</span> {name}</p>
      <h1>{name}</h1>
      <p className="description">{description}</p>
    </header>
    {children}
  </main>;
}

export function RecordWorkspace({ selected, children }: { selected: string | null; children: ReactNode }) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const list = container.current?.querySelector<HTMLElement>(".record-list .table-wrap");
    const row = list?.querySelector('tr[data-selected="true"]');
    if (!selected || !list || !row) return;
    const bounds = list.getBoundingClientRect();
    const headerHeight = list.querySelector("thead")?.clientHeight ?? 0;
    const current = row.getBoundingClientRect();
    if (current.bottom > bounds.bottom) list.scrollTop += current.bottom - bounds.bottom;
    else if (current.top < bounds.top + headerHeight) list.scrollTop -= bounds.top + headerHeight - current.top;
  }, [selected]);
  return <div ref={container} className={selected ? "record-workspace has-inspector" : "record-workspace"}>{children}</div>;
}

const statusTones: Record<string, string> = {
  succeeded: "positive", available: "positive", healthy: "positive", published: "positive",
  enabled: "positive", approved: "positive", allow: "positive",
  denied: "blocked", failed: "blocked", rejected: "blocked", unhealthy: "blocked", deny: "blocked",
  quarantined: "blocked", unavailable: "blocked",
  pending: "attention", pending_approval: "attention", running: "attention", degraded: "attention",
  queued: "attention", unknown: "attention", received: "attention",
};

export function StatusLabel({ value }: { value: string }) {
  return <span className="record-status" data-tone={statusTones[value.toLowerCase()] ?? "neutral"}>
    {value}
  </span>;
}

export function State({ loading, error, retry, children }: {
  loading: boolean; error: Error | null; retry: () => unknown; children?: ReactNode;
}) {
  if (loading) return <p className="workspace-feedback" role="status">Loading…</p>;
  if (error) return <div className="workspace-feedback inline-controls"><p role="alert">{error.message}</p><button onClick={retry}>Retry</button></div>;
  return children;
}
