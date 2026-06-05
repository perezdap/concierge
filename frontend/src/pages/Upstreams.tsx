import { useCallback, useEffect, useState } from "react";
import { PendingChanges } from "../../components/PendingChanges";
import {
  api,
  ApiError,
  type TestConnectionResult,
  type UpstreamRecord,
  type UpstreamTransport,
} from "../api";

const TRANSPORTS: UpstreamTransport[] = ["stdio", "streamable_http", "sse_legacy", "custom"];

/** Matches backend ``REDACTED_VALUE`` and write-only display forms. */
const REDACTED_HEADER = "***";
const WRITE_ONLY_HEADER = { __write_only__: true as const };

function emptyUpstream(): UpstreamRecord {
  return {
    id: "",
    transport: "stdio",
    command: ["python", ""],
    headers: {},
    default_tags: [],
    default_categories: [],
    isolation: "shared",
  };
}

function commandToText(cmd: string[] | null | undefined): string {
  return (cmd ?? []).join(" ");
}

function textToCommand(text: string): string[] {
  return text.trim() ? text.trim().split(/\s+/) : [];
}

function headersToText(headers: Record<string, string> | undefined): string {
  if (!headers || Object.keys(headers).length === 0) {
    return "";
  }
  return Object.entries(headers)
    .map(([k, v]) => `${k}: ${v}`)
    .join("\n");
}

function textToHeaders(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const idx = trimmed.indexOf(":");
    if (idx < 1) continue;
    out[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
  }
  return out;
}

function isPreservedHeaderValue(value: string): boolean {
  return (
    value === REDACTED_HEADER ||
    value === "[REDACTED]" ||
    value.startsWith("secret_ref:")
  );
}

function prepareHeadersForSave(
  edited: Record<string, string>,
  baseline: Record<string, string>,
): Record<string, string | typeof WRITE_ONLY_HEADER> {
  const out: Record<string, string | typeof WRITE_ONLY_HEADER> = {};
  for (const [key, val] of Object.entries(edited)) {
    const prior = baseline[key];
    const priorWasSecret =
      prior !== undefined &&
      (isPreservedHeaderValue(prior) || prior.startsWith("secret_ref:"));
    if (isPreservedHeaderValue(val) && priorWasSecret) {
      out[key] = WRITE_ONLY_HEADER;
    } else if (prior !== undefined && val === prior && prior.startsWith("secret_ref:")) {
      out[key] = WRITE_ONLY_HEADER;
    } else {
      out[key] = val;
    }
  }
  return out;
}

export default function Upstreams() {
  const [list, setList] = useState<UpstreamRecord[]>([]);
  const [source, setSource] = useState<string>("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [form, setForm] = useState<UpstreamRecord>(emptyUpstream());
  const [isNew, setIsNew] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(null);
  const [refreshMsg, setRefreshMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [baselineHeaders, setBaselineHeaders] = useState<Record<string, string>>({});

  const loadList = useCallback(async () => {
    setError(null);
    try {
      const res = await api.listUpstreams();
      setList(res.upstreams);
      setSource(res.source);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Load failed");
    }
  }, []);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  const selectUpstream = async (id: string) => {
    setIsNew(false);
    setSelectedId(id);
    setTestResult(null);
    setRefreshMsg(null);
    setError(null);
    try {
      const res = await api.getUpstream(id);
      setForm(res.upstream);
      setBaselineHeaders({ ...(res.upstream.headers ?? {}) });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Load failed");
    }
  };

  const startNew = () => {
    setIsNew(true);
    setSelectedId(null);
    setForm(emptyUpstream());
    setBaselineHeaders({});
    setTestResult(null);
    setRefreshMsg(null);
  };

  const save = async () => {
    if (!form.id.trim()) {
      setError("Upstream id is required");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const payload = {
        ...form,
        id: form.id.trim(),
        headers: prepareHeadersForSave(
          form.headers ?? {},
          baselineHeaders,
        ) as unknown as Record<string, string>,
      };
      if (isNew) {
        await api.createUpstream(payload);
      } else if (selectedId) {
        await api.updateUpstream(selectedId, payload);
      }
      await loadList();
      setIsNew(false);
      setSelectedId(payload.id);
      const refreshed = await api.getUpstream(payload.id);
      setForm(refreshed.upstream);
      setBaselineHeaders({ ...(refreshed.upstream.headers ?? {}) });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!selectedId || isNew) return;
    if (!window.confirm(`Delete upstream "${selectedId}" from draft?`)) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteUpstream(selectedId);
      setSelectedId(null);
      setForm(emptyUpstream());
      await loadList();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Delete failed");
    } finally {
      setBusy(false);
    }
  };

  const testConnection = async () => {
    const id = form.id.trim();
    if (!id) {
      setError("Set an upstream id before testing");
      return;
    }
    setBusy(true);
    setError(null);
    setTestResult(null);
    setRefreshMsg(null);
    try {
      const res = await api.testUpstreamConnection(id, form);
      setTestResult(res);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Test failed");
    } finally {
      setBusy(false);
    }
  };

  const refreshCatalog = async () => {
    const id = form.id.trim();
    if (!id) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.refreshUpstreamCatalog(id);
      setRefreshMsg(`Catalog refreshed: ${res.entries} entries for ${res.server}.`);
    } catch (e) {
      setError(
        e instanceof ApiError
          ? `${e.message} — apply draft first if this upstream is new.`
          : e instanceof Error
            ? e.message
            : "Refresh failed",
      );
    } finally {
      setBusy(false);
    }
  };

  const patch = (partial: Partial<UpstreamRecord>) => setForm((f) => ({ ...f, ...partial }));

  return (
    <div>
      <h1 className="page-title">Upstreams</h1>
      <p className="page-subtitle">
        Manage upstream servers on the config draft ({source || "…"}). Secrets in headers are
        redacted from API responses.
      </p>

      {error ? <div className="banner error">{error}</div> : null}

      <PendingChanges
        applyDraft={api.applyUpstreamsDraft}
        onApplied={() => {
          void loadList();
          if (selectedId) void selectUpstream(selectedId);
        }}
      />

      <div className="split-layout">
        <aside className="split-list">
          <div className="btn-row">
            <button type="button" onClick={startNew}>
              Add upstream
            </button>
            <button type="button" onClick={() => void loadList()}>
              Refresh list
            </button>
          </div>
          <ul className="item-list">
            {list.map((u) => (
              <li key={u.id}>
                <button
                  type="button"
                  className={selectedId === u.id && !isNew ? "list-active" : ""}
                  onClick={() => void selectUpstream(u.id)}
                >
                  <strong>{u.id}</strong>
                  <span className="muted-text">{u.transport}</span>
                </button>
              </li>
            ))}
            {list.length === 0 ? <li className="muted-text">No upstreams</li> : null}
          </ul>
        </aside>

        <section className="split-editor">
          <h2>{isNew ? "New upstream" : selectedId ? `Edit ${selectedId}` : "Select an upstream"}</h2>
          {(isNew || selectedId) && (
            <>
              <div className="form-grid">
                <label>
                  ID
                  <input
                    value={form.id}
                    disabled={!isNew}
                    onChange={(e) => patch({ id: e.target.value })}
                  />
                </label>
                <label>
                  Transport
                  <select
                    value={form.transport}
                    onChange={(e) => patch({ transport: e.target.value as UpstreamTransport })}
                  >
                    {TRANSPORTS.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                </label>

                {form.transport === "stdio" ? (
                  <label className="span-2">
                    Command (space-separated)
                    <input
                      value={commandToText(form.command)}
                      onChange={(e) => patch({ command: textToCommand(e.target.value) })}
                    />
                  </label>
                ) : null}

                {form.transport === "streamable_http" ? (
                  <label className="span-2">
                    URL
                    <input
                      value={form.url ?? ""}
                      onChange={(e) => patch({ url: e.target.value })}
                    />
                  </label>
                ) : null}

                {form.transport === "sse_legacy" ? (
                  <>
                    <label>
                      SSE URL
                      <input
                        value={form.sse_url ?? ""}
                        onChange={(e) => patch({ sse_url: e.target.value })}
                      />
                    </label>
                    <label>
                      POST URL
                      <input
                        value={form.post_url ?? ""}
                        onChange={(e) => patch({ post_url: e.target.value })}
                      />
                    </label>
                  </>
                ) : null}

                {form.transport === "custom" ? (
                  <label className="span-2">
                    Custom kind
                    <input
                      value={form.custom_kind ?? ""}
                      onChange={(e) => patch({ custom_kind: e.target.value })}
                    />
                  </label>
                ) : null}

                {(form.transport === "streamable_http" ||
                  form.transport === "sse_legacy") && (
                  <label className="span-2">
                    Headers (one per line, values shown redacted from server)
                    <textarea
                      rows={4}
                      value={headersToText(form.headers)}
                      onChange={(e) => patch({ headers: textToHeaders(e.target.value) })}
                      placeholder="Authorization: secret_ref:..."
                    />
                    {Object.values(form.headers ?? {}).some(isPreservedHeaderValue) ? (
                      <span className="hint-text">
                        Contains write-only / redacted values — leave unchanged or set a new
                        secret_ref on save.
                      </span>
                    ) : null}
                  </label>
                )}

                <label className="span-2">
                  Default tags (comma-separated)
                  <input
                    value={(form.default_tags ?? []).join(", ")}
                    onChange={(e) =>
                      patch({
                        default_tags: e.target.value
                          .split(",")
                          .map((s) => s.trim())
                          .filter(Boolean),
                      })
                    }
                  />
                </label>
              </div>

              <div className="btn-row">
                <button type="button" onClick={() => void save()} disabled={busy}>
                  Save to draft
                </button>
                <button type="button" onClick={() => void testConnection()} disabled={busy}>
                  Test connection
                </button>
                <button
                  type="button"
                  onClick={() => void refreshCatalog()}
                  disabled={busy || isNew}
                >
                  Refresh catalog
                </button>
                {!isNew ? (
                  <button type="button" className="btn-danger" onClick={() => void remove()} disabled={busy}>
                    Delete
                  </button>
                ) : null}
              </div>

              {refreshMsg ? (
                <article className="card">
                  <h2>Catalog refresh</h2>
                  <p className="ok-text">{refreshMsg}</p>
                </article>
              ) : null}

              {testResult ? (
                <article className="card">
                  <h2>Connection result</h2>
                  <p className={testResult.ok ? "ok-text" : "bad-text"}>
                    {testResult.ok ? "OK" : "Failed"}
                    {testResult.latency_ms != null ? ` · ${testResult.latency_ms} ms` : ""}
                  </p>
                  {testResult.ok ? (
                    <p className="muted-text">
                      Tools {testResult.tools_discovered ?? 0} · Resources{" "}
                      {testResult.resources_discovered ?? 0} · Prompts{" "}
                      {testResult.prompts_discovered ?? 0}
                    </p>
                  ) : null}
                  {testResult.error ? <p className="bad-text">{testResult.error}</p> : null}
                </article>
              ) : null}
            </>
          )}
        </section>
      </div>
    </div>
  );
}