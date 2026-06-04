import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  type AdapterHealth,
  type AdminHealth,
  type ConfigStatus,
  type ReadyzResponse,
} from "../api";

interface DashboardData {
  healthz: boolean;
  readyz: ReadyzResponse;
  admin: AdminHealth;
  catalogCount: number;
  sessionCount: number;
  config: ConfigStatus;
  issues: string[];
}

function serverId(row: AdapterHealth): string {
  return row.server_id ?? "unknown";
}

function collectIssues(
  readyz: ReadyzResponse,
  servers: AdapterHealth[],
): string[] {
  const issues: string[] = [];
  if (readyz.draining) {
    issues.push("Gateway is draining — readiness reports not-ready.");
  }
  for (const s of servers) {
    if (s.connected === false) {
      issues.push(`Upstream ${serverId(s)} is disconnected.`);
    }
    if (s.last_error) {
      issues.push(`${serverId(s)}: ${s.last_error}`);
    }
    if (s.circuit_open) {
      issues.push(`${serverId(s)} circuit breaker open`);
    }
  }
  return issues.slice(0, 12);
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [healthz, readyz, admin, catalog, sessions, config] = await Promise.all([
        api.healthz(),
        api.readyz(),
        api.adminHealth(),
        api.catalog(),
        api.sessions(),
        api.configStatus(),
      ]);
      const servers = admin.servers.length ? admin.servers : (config.servers ?? []);
      setData({
        healthz: healthz.ok,
        readyz,
        admin,
        catalogCount: catalog.entries.length,
        sessionCount: sessions.sessions.length,
        config,
        issues: collectIssues(readyz, servers),
      });
      setLastRefresh(new Date());
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? `${e.message} (${e.status})`
          : e instanceof Error
            ? e.message
            : "Failed to load dashboard";
      setError(msg);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), 30_000);
    return () => window.clearInterval(id);
  }, [load]);

  const servers = data?.admin.servers ?? data?.config.servers ?? [];
  const readyOk = data?.readyz.ok ?? false;

  return (
    <div>
      <h1 className="page-title">Dashboard</h1>
      <p className="page-subtitle">
        Gateway health, upstream status, catalog, sessions, and runtime config.
      </p>

      <div className="refresh-row">
        <span className="muted">
          {lastRefresh
            ? `Updated ${lastRefresh.toLocaleTimeString()}`
            : loading
              ? "Loading…"
              : "Not loaded"}
        </span>
        <button type="button" onClick={() => void load()} disabled={loading}>
          Refresh
        </button>
      </div>

      {error ? <div className="banner error">{error}</div> : null}

      {data ? (
        <div className="card-grid">
          <article className="card">
            <h2>Liveness</h2>
            <div className={`metric ${data.healthz ? "ok" : "bad"}`}>
              {data.healthz ? "OK" : "Down"}
            </div>
            <p>/healthz</p>
          </article>

          <article className="card">
            <h2>Readiness</h2>
            <div className={`metric ${readyOk ? "ok" : "warn"}`}>
              {readyOk ? "Ready" : "Not ready"}
            </div>
            <span className={`status-pill ${readyOk ? "ok" : "bad"}`}>
              {data.readyz.draining ? "draining" : readyOk ? "all upstreams up" : "degraded"}
            </span>
          </article>

          <article className="card">
            <h2>Catalog</h2>
            <div className="metric ok">{data.catalogCount}</div>
            <p>Primitives registered</p>
          </article>

          <article className="card">
            <h2>Active sessions</h2>
            <div className="metric ok">{data.sessionCount}</div>
            <p>From /admin/sessions</p>
          </article>

          <article className="card">
            <h2>Config version</h2>
            <div className="metric" style={{ fontSize: "1rem" }}>
              {data.config.active_version_id ?? "—"}
            </div>
            <p>
              Draft: {data.config.draft_version_id ?? "none"}
              {data.config.draft_status ? ` (${data.config.draft_status})` : ""}
            </p>
            <p>Reload coordinator: {data.config.reload_coordinator ? "yes" : "no"}</p>
          </article>

          <article className="card">
            <h2>Admin health</h2>
            <div className={`metric ${data.admin.ok ? "ok" : "bad"}`}>
              {data.admin.ok ? "OK" : "Issue"}
            </div>
            <p>
              {data.admin.catalog_count} catalog · {data.admin.session_count} sessions
            </p>
          </article>

          <article className="card card-wide">
            <h2>Upstream status</h2>
            {servers.length === 0 ? (
              <p>No upstream servers registered.</p>
            ) : (
              <table className="server-table">
                <thead>
                  <tr>
                    <th>Server</th>
                    <th>Connected</th>
                    <th>Circuit</th>
                    <th>Last error</th>
                  </tr>
                </thead>
                <tbody>
                  {servers.map((s) => (
                    <tr key={serverId(s)}>
                      <td>{serverId(s)}</td>
                      <td>{s.connected ? "yes" : "no"}</td>
                      <td>{s.circuit_open ? "open" : "closed"}</td>
                      <td>{s.last_error ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </article>

          <article className="card card-wide">
            <h2>Recent issues</h2>
            {data.issues.length === 0 ? (
              <p>No upstream or readiness issues detected. Audit stream API not exposed yet.</p>
            ) : (
              <ul>
                {data.issues.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            )}
          </article>
        </div>
      ) : null}
    </div>
  );
}