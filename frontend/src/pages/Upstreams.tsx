import { useCallback, useEffect, useState } from "react";
import { PendingChanges } from "../../components/PendingChanges";
import { OAuthConnect } from "../../components/OAuthConnect";
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
import {
  envToText,
  isPreservedEnvValue,
  prepareEnvForSave,
  validateEnvText,
} from "../upstreamEnv";
import { FieldLabel } from "../fieldHelp";

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
  const [commandText, setCommandText] = useState("");
  const [envText, setEnvText] = useState("");
  const [headersText, setHeadersText] = useState("");
  const [isNew, setIsNew] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(null);
  const [refreshMsg, setRefreshMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [baselineHeaders, setBaselineHeaders] = useState<Record<string, string>>({});
  const [baselineEnv, setBaselineEnv] = useState<Record<string, string>>({});

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
      setCommandText(commandToText(res.upstream.command));
      setEnvText(envToText(res.upstream.env ?? undefined));
      setBaselineEnv({ ...(res.upstream.env ?? {}) });
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
    setCommandText("");
    setEnvText("");
    setHeadersText("");
    setBaselineEnv({});
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

  const parseEnvForSubmit = (): Record<string, string> | null => {
    const { headers: env, errors } = validateEnvText(envText);
    if (errors.length > 0) {
      setError(errors.join("; "));
      return null;
    }
    return env;
  };

  const buildUpstreamPayload = () => {
    const isStdio = form.transport === "stdio";
    const usesHeaders =
      form.transport === "streamable_http" || form.transport === "sse_legacy";

    const parsedHeaders = usesHeaders ? parseHeadersForSubmit() : {};
    if (usesHeaders && parsedHeaders === null) {
      return null;
    }

    const parsedEnv = isStdio ? parseEnvForSubmit() : null;
    if (isStdio && parsedEnv === null) {
      return null;
    }

    const preparedEnv =
      isStdio && parsedEnv
        ? (prepareEnvForSave(parsedEnv, baselineEnv) as Record<string, string>)
        : null;

    return {
      ...form,
      id: form.id.trim(),
      command: isStdio ? textToCommand(commandText) : form.command,
      env: isStdio && preparedEnv && Object.keys(preparedEnv).length > 0 ? preparedEnv : null,
      default_tags: (form.default_tags ?? []).filter(Boolean),
      headers: usesHeaders
        ? (prepareHeadersForSave(parsedHeaders!, baselineHeaders) as unknown as Record<
            string,
            string
          >)
        : {},
    };
  };

  const save = async () => {
    if (!form.id.trim()) {
      setError("Upstream id is required");
      return;
    }
    const payload = buildUpstreamPayload();
    if (payload === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
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
      setCommandText(commandToText(refreshed.upstream.command));
      setEnvText(envToText(refreshed.upstream.env ?? undefined));
      setBaselineEnv({ ...(refreshed.upstream.env ?? {}) });
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
      setCommandText("");
      setEnvText("");
      setHeadersText("");
      setBaselineEnv({});
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
    const isStdio = form.transport === "stdio";
    const usesHeaders =
      form.transport === "streamable_http" || form.transport === "sse_legacy";

    const parsedHeaders = usesHeaders ? parseHeadersForSubmit() : {};
    if (usesHeaders && parsedHeaders === null) {
      return;
    }

    const parsedEnv = isStdio ? parseEnvForSubmit() : null;
    if (isStdio && parsedEnv === null) {
      return;
    }

    setBusy(true);
    setError(null);
    setTestResult(null);
    setRefreshMsg(null);
    try {
      const res = await api.testUpstreamConnection(id, {
        ...form,
        command: isStdio ? textToCommand(commandText) : form.command,
        env: isStdio && parsedEnv && Object.keys(parsedEnv).length > 0 ? parsedEnv : null,
        default_tags: (form.default_tags ?? []).filter(Boolean),
        headers: usesHeaders ? parsedHeaders! : {},
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
        Manage upstream servers on the config draft ({source || "…"}). Headers and stdio
        environment values are redacted from API responses.
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
                <FieldLabel label="ID" helpKey="upstreamId">
                  <input
                    value={form.id}
                    disabled={!isNew}
                    onChange={(e) => patch({ id: e.target.value })}
                  />
                </FieldLabel>
                <FieldLabel label="Transport" helpKey="upstreamTransport">
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
                </FieldLabel>

                {form.transport === "stdio" ? (
                  <>
                    <FieldLabel label="Command (space-separated)" helpKey="upstreamCommand" className="span-2">
                      <input
                        value={commandText}
                        onChange={(e) => setCommandText(e.target.value)}
                      />
                    </FieldLabel>
                    <FieldLabel
                      label="Environment (one NAME: value per line, redacted from server)"
                      helpKey="upstreamEnv"
                      className="span-2"
                    >
                      <textarea
                        rows={4}
                        value={envText}
                        onChange={(e) => setEnvText(e.target.value)}
                        placeholder="BRAVE_API_KEY: your-key-here"
                      />
                      {Object.values(baselineEnv).some(isPreservedEnvValue) ? (
                        <span className="hint-text">
                          Contains write-only / redacted values — leave unchanged or enter a new
                          value on save.
                        </span>
                      ) : null}
                    </FieldLabel>
                  </>
                ) : null}

                {form.transport === "streamable_http" ? (
                  <FieldLabel label="URL" helpKey="upstreamUrl" className="span-2">
                    <input
                      value={form.url ?? ""}
                      onChange={(e) => patch({ url: e.target.value })}
                    />
                  </FieldLabel>
                ) : null}

                {form.transport === "sse_legacy" ? (
                  <>
                    <FieldLabel label="SSE URL" helpKey="upstreamSseUrl">
                      <input
                        value={form.sse_url ?? ""}
                        onChange={(e) => patch({ sse_url: e.target.value })}
                      />
                    </FieldLabel>
                    <FieldLabel label="POST URL" helpKey="upstreamPostUrl">
                      <input
                        value={form.post_url ?? ""}
                        onChange={(e) => patch({ post_url: e.target.value })}
                      />
                    </FieldLabel>
                  </>
                ) : null}

                {form.transport === "custom" ? (
                  <FieldLabel label="Custom kind" helpKey="upstreamCustomKind" className="span-2">
                    <input
                      value={form.custom_kind ?? ""}
                      onChange={(e) => patch({ custom_kind: e.target.value })}
                    />
                  </FieldLabel>
                ) : null}

                {(form.transport === "streamable_http" ||
                  form.transport === "sse_legacy") && (
                  <FieldLabel
                    label="Headers (one per line, values shown redacted from server)"
                    helpKey="upstreamHeaders"
                    className="span-2"
                  >
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
                  </FieldLabel>
                )}

                <FieldLabel label="Default tags (comma-separated)" helpKey="upstreamDefaultTags" className="span-2">
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
                </FieldLabel>
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

              {!isNew &&
              selectedId &&
              (form.transport === "streamable_http" || form.transport === "sse_legacy") ? (
                <OAuthConnect
                  upstreamId={selectedId}
                  resourceUrl={form.url ?? form.sse_url ?? null}
                />
              ) : null}

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
