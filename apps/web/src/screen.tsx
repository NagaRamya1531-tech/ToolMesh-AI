import type { ReactNode } from "react";

export function Screen({ name, description, children }: { name: string; description: string; children?: ReactNode }) {
  return <main id="main" tabIndex={-1}>
    <p className="breadcrumb">Workspace / {name}</p><h1>{name}</h1>
    <p className="description">{description}</p>{children}
  </main>;
}

export function State({ loading, error, retry, children }: {
  loading: boolean; error: Error | null; retry: () => unknown; children?: ReactNode;
}) {
  if (loading) return <p role="status">Loading…</p>;
  if (error) return <div className="inline-controls"><p role="alert">{error.message}</p><button onClick={retry}>Retry</button></div>;
  return children;
}
