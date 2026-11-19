import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, NavLink, Route, Routes } from "react-router-dom";
import { call, getReadiness } from "./api";
import { Executions, Playground, Registry, Services, type Session, SignIn } from "./workspace";
import { Approvals, Audit, OverviewActivity } from "./governance";
import { DuplicateWorkflow } from "./duplicate-workflow";
import { Crawler } from "./crawler";
import { Evaluations } from "./evaluations";
import { Agents, Policies, Settings } from "./access";
import { AgentScenarios } from "./agent-scenarios";
import "./styles.css";

const groups = [
  ["Platform", "Overview", "Services", "Registry", "AutoCrawler"],
  ["Access", "Agents", "Policies", "Approvals"],
  ["Operations", "Playground", "Executions", "Audit"],
  ["Quality", "Evaluations"],
  ["Administration", "Settings"],
];
const queryClient = new QueryClient();

const routes: Record<string, string> = {
  Overview: "/", Services: "/services", Registry: "/tools", Playground: "/playground", Executions: "/executions", Approvals: "/approvals", Audit: "/audit", AutoCrawler: "/crawler", Evaluations: "/evaluations", Agents: "/agents", Policies: "/policies", Settings: "/settings",
};

function Navigation({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="Console">
      {groups.map(([group, ...items]) => (
        <section key={group}>
          <h2>{group}</h2>
          <ul>{items.map((item) => (
            <li key={item}>{routes[item]
              ? <NavLink to={routes[item]} end onClick={onNavigate}>{item}</NavLink>
              : <span className="planned">{item}<small>Planned</small></span>}
            </li>
          ))}</ul>
        </section>
      ))}
    </nav>
  );
}

function Overview({ session }: { session: Session | null }) {
  const readiness = useQuery({
    queryKey: ["readiness"],
    queryFn: ({ signal }) => getReadiness(signal),
    retry: false,
    refetchInterval: 30_000,
  });
  const { data, isFetching, isError, isPending } = readiness;
  const status = isError ? "Gateway unreachable" : isPending ? "Checking dependencies" :
    data?.status === "ready" ? "Ready" : "Degraded";
  const tone = isError || data?.status === "unready" ? "warning" : data ? "ok" : "neutral";

  return (
    <main id="main" tabIndex={-1}>
      <p className="breadcrumb">Platform / Overview</p>
      <h1>Overview</h1>
      <p className="description">Monitor gateway readiness and its backing services.</p>
      <section className="panel" aria-labelledby="status-heading">
        <header className="panel-heading">
          <div>
            <h2 id="status-heading">System status</h2>
            <p role="status" className={tone}>{status}</p>
          </div>
          <button type="button" disabled={isFetching} onClick={() => void readiness.refetch()}>
            {isFetching ? "Checking…" : isError ? "Retry" : "Refresh"}
          </button>
        </header>
        {isError && <p className="notice">The readiness check failed. {data && "Showing the last successful check."}</p>}
        <table>
          <thead><tr><th scope="col">Dependency</th><th scope="col">Status</th></tr></thead>
          <tbody>
            <tr><th scope="row">PostgreSQL and pgvector</th><td>{data?.checks.postgres ?? "Unknown"}</td></tr>
            <tr><th scope="row">Redis</th><td>{data?.checks.redis ?? "Unknown"}</td></tr>
          </tbody>
        </table>
        <footer>{data
          ? <>Last checked <time dateTime={data.checked_at}>{new Date(data.checked_at).toLocaleString()}</time></>
          : "Waiting for a readiness response"}</footer>
      </section>
      {session && <OverviewActivity session={session} />}
    </main>
  );
}

function App() {
  const drawer = useRef<HTMLDialogElement>(null);
  const [sidebarExpanded, setSidebarExpanded] = useState(true);
  const [session, setSession] = useState<Session | null>(null);
  const [signOutError, setSignOutError] = useState("");
  const [signingOut, setSigningOut] = useState(false);
  async function signOut() {
    if (!session) return;
    setSigningOut(true);
    setSignOutError("");
    try { await call("/auth/logout", session.token, {}); }
    catch { setSignOutError("Signed out locally. The server session may remain active until expiry."); }
    finally {
      queryClient.removeQueries({ predicate: ({ queryKey }) => queryKey[0] !== "readiness" });
      setSession(null);
      setSigningOut(false);
    }
  }
  function secure(Page: typeof Services) {
    return session ? <Page session={session} /> :
      <SignIn onSession={(value) => {setSignOutError(""); setSession(value);}} />;
  }
  return (
    <div className={sidebarExpanded ? "shell" : "shell sidebar-collapsed"}>
      <a className="skip" href="#main">Skip to content</a>
      <aside className="sidebar" id="desktop-sidebar"><Link className="brand" to="/">ToolMesh AI</Link><Navigation /></aside>
      <div className="content">
        <header className="topbar">
          <button className="desktop-menu" type="button" aria-controls="desktop-sidebar"
            aria-expanded={sidebarExpanded} onClick={() => setSidebarExpanded((expanded) => !expanded)}>
            {sidebarExpanded ? "Hide sidebar" : "Show sidebar"}
          </button>
          <button className="mobile-menu" type="button" onClick={() => drawer.current?.showModal()}>
            Menu
          </button>
          <span>Workspace <strong>Sandbox</strong></span>
          <span className="environment">Local</span>
          {session
            ? <div className="account">
                <span className="account-name">{session.id}</span>
                <button type="button" disabled={signingOut} onClick={() => void signOut()}>Sign out</button>
              </div>
            : <Link className="top-link" to="/services">Sign in</Link>}
        </header>
        {signOutError && <p className="notice" role="alert">{signOutError}</p>}
        <Routes>
          <Route path="/" element={<Overview session={session} />} />
          <Route path="/services" element={secure(Services)} />
          <Route path="/tools" element={secure(Registry)} />
          <Route path="/playground" element={secure(Playground)} />
          <Route path="/playground/scenarios" element={secure(AgentScenarios)} />
          <Route path="/playground/duplicate-payment" element={secure(DuplicateWorkflow)} />
          <Route path="/executions" element={secure(Executions)} />
          <Route path="/approvals" element={secure(Approvals)} />
          <Route path="/audit" element={secure(Audit)} />
          <Route path="/crawler" element={secure(Crawler)} />
          <Route path="/evaluations" element={secure(Evaluations)} />
          <Route path="/agents" element={secure(Agents)} />
          <Route path="/policies" element={secure(Policies)} />
          <Route path="/settings" element={secure(Settings)} />
          <Route path="*" element={<main id="main" tabIndex={-1}><h1>Page unavailable</h1><Link to="/">Back to Overview</Link></main>} />
        </Routes>
      </div>
      <dialog ref={drawer} aria-label="Console navigation">
        <form method="dialog"><button>Close menu</button></form>
        <Navigation onNavigate={() => drawer.current?.close()} />
      </dialog>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}><BrowserRouter><App /></BrowserRouter></QueryClientProvider>,
);
