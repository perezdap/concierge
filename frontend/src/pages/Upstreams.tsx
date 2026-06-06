import { useCallback, useEffect, useState } from "react";
import { PendingChanges } from "../../components/PendingChanges";
import {
  api,
  ApiError,
  type TestConnectionResult,
  type UpstreamRecord,
  type UpstreamTransport,
} from "../api";
import {
  headersToText,
  isPreservedHeaderValue,
  prepareHeadersForSave,
  validateHeadersText,
} from "../upstreamHeaders";

const TRANSPORTS: UpstreamTransport[] = ["stdio", "streamable_http", "sse_legacy", "custom"];

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

export default function Upstreams() {
  const [list, setList] = useState<UpstreamRecord[]>([]);
  const [source, setSource] = useState<string>("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [form, setForm] = useState<UpstreamRecord>(emptyUpstream());
  const [headersText, setHeadersText] = useState("");
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
      setHeadersText(headersToText(res.upstream.headers));
      setBaselineHeaders({ ...(res.upstream.headers ?? {}) });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Load failed");
    }
  };

  const startNew = () => {
    setIsNew(true);
    setSelectedId(null);
    setForm(emptyUpstream());
    setHeadersText("");
    setBaselineHeaders({});
    setTestResult(null);
    setRefreshMsg(null);
  };

  const parseHeadersForSubmit = (): Record<string, string> | null => {
    const { headers, errors } = validateHeadersText(headersText);
    if (errors.length > 0) {
      setError(errors.join("; "));
      return null;
    }
    return headers;
  };

  const save = async () => {
    if (!form.id.trim()) {
      setError("Upstream id is required");
      return;
    }
    const parsedHeaders = parseHeadersForSubmit();
    if (parsedHeaders === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const payload = {
        ...form,
        id: form.id.trim(),
        default_tags: (form.default_tags ?? []).filter(Boolean),
        headers: prepareHeadersForSave(parsedHeaders, baselineHeaders) as unknown as Record<
          string,
          string
        >,
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
      setHeadersText(headersToText(refreshed.upstream.headers));
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
      setHeadersText("");
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
    const parsedHeaders = parseHeadersForSubmit();
    if (parsedHeaders === null) {
      return;
    }
    setBusy(true);
    setError(null);
    setTestResult(null);
    setRefreshMsg(null);
    try {
      const res = await api.testUpstreamConnection(id, {
        ...form,
        default_tags: (form.default_tags ?? []).filter(Boolean),
        headers: parsedHeaders,
      });
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
                      value={headersText}
                      onChange={(e) => setHeadersText(e.target.value)}
                      placeholder="Authorization: secret_ref:..."
                    />
                    {Object.values(baselineHeaders).some(isPreservedHeaderValue) ? (
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
                          .map((s) => s.trim()),
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
