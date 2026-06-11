import { useCallback, useEffect, useState } from "react";
import { PendingChanges } from "../../components/PendingChanges";
import { FieldHelp } from "../../components/FieldHelp";
import { PROFILE_HELP } from "../adminFieldHelp";
import {
  api,
  ApiError,
  type ProfilePreviewResult,
  type ProfileRecord,
  type ProfileSelector,
} from "../api";

const PRIMITIVE_TYPES = ["", "tool", "resource", "prompt"] as const;

function primitiveDisplayName(entry: { canonical_name?: string; name?: string }): string {
  return entry.canonical_name ?? entry.name ?? "—";
}

function emptyProfile(): ProfileRecord {
  return {
    name: "",
    description: "",
    selectors: [],
    auto_apply: false,
  };
}

function emptySelector(): ProfileSelector {
  return { server: "", tags: [], categories: [], names: [], primitive_type: null };
}

function csvToList(text: string): string[] {
  return text
    .split(",")
    .map((s) => s.trim());
}

export default function Profiles() {
  const [list, setList] = useState<ProfileRecord[]>([]);
  const [source, setSource] = useState("");
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [form, setForm] = useState<ProfileRecord>(emptyProfile());
  const [isNew, setIsNew] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<ProfilePreviewResult | null>(null);
  const [busy, setBusy] = useState(false);

  const loadList = useCallback(async () => {
    setError(null);
    try {
      const res = await api.listProfiles();
      setList(res.profiles);
      setSource(res.source);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Load failed");
    }
  }, []);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  const selectProfile = async (name: string) => {
    setIsNew(false);
    setSelectedName(name);
    setPreview(null);
    setError(null);
    try {
      const res = await api.getProfile(name);
      setForm(res.profile);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Load failed");
    }
  };

  const startNew = () => {
    setIsNew(true);
    setSelectedName(null);
    setForm(emptyProfile());
    setPreview(null);
  };

  const save = async () => {
    if (!form.name.trim()) {
      setError("Profile name is required");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const cleanSelectors = (form.selectors ?? []).map((sel) => ({
        ...sel,
        tags: (sel.tags ?? []).filter(Boolean),
        categories: (sel.categories ?? []).filter(Boolean),
        names: (sel.names ?? []).filter(Boolean),
      }));
      const payload: ProfileRecord = {
        ...form,
        name: form.name.trim(),
        selectors: cleanSelectors,
      };
      if (isNew) {
        await api.createProfile(payload);
      } else if (selectedName) {
        await api.updateProfile(selectedName, payload);
      }
      await loadList();
      setIsNew(false);
      setSelectedName(payload.name);
      const refreshed = await api.getProfile(payload.name);
      setForm(refreshed.profile);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!selectedName || isNew) return;
    if (!window.confirm(`Delete profile "${selectedName}" from draft?`)) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteProfile(selectedName);
      setSelectedName(null);
      setForm(emptyProfile());
      setPreview(null);
      await loadList();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Delete failed");
    } finally {
      setBusy(false);
    }
  };

  const duplicate = async () => {
    if (!selectedName || isNew) return;
    const newName = window.prompt("New profile name:", `${selectedName}-copy`);
    if (!newName?.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.duplicateProfile(selectedName, newName.trim());
      await loadList();
      await selectProfile(newName.trim());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Duplicate failed");
    } finally {
      setBusy(false);
    }
  };

  const runPreview = async () => {
    const name = form.name.trim();
    if (!name) {
      setError("Set a profile name before preview");
      return;
    }
    setBusy(true);
    setError(null);
    setPreview(null);
    try {
      const cleanSelectors = (form.selectors ?? []).map((sel) => ({
        ...sel,
        tags: (sel.tags ?? []).filter(Boolean),
        categories: (sel.categories ?? []).filter(Boolean),
        names: (sel.names ?? []).filter(Boolean),
      }));
      const res = await api.previewProfile(name, {
        ...form,
        selectors: cleanSelectors,
      });
      setPreview(res);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Preview failed");
    } finally {
      setBusy(false);
    }
  };

  const patch = (partial: Partial<ProfileRecord>) => setForm((f) => ({ ...f, ...partial }));

  const updateSelector = (index: number, partial: Partial<ProfileSelector>) => {
    const selectors = [...(form.selectors ?? [])];
    selectors[index] = { ...selectors[index], ...partial };
    patch({ selectors });
  };

  const addSelector = () => {
    patch({ selectors: [...(form.selectors ?? []), emptySelector()] });
  };

  const removeSelector = (index: number) => {
    const selectors = [...(form.selectors ?? [])];
    selectors.splice(index, 1);
    patch({ selectors });
  };

  return (
    <div>
      <h1 className="page-title">Profiles</h1>
      <p className="page-subtitle">
        Profile selectors resolve against the live catalog ({source || "…"}).
      </p>

      {error ? <div className="banner error">{error}</div> : null}

      <PendingChanges
        applyDraft={api.applyProfilesDraft}
        onApplied={() => {
          void loadList();
          if (selectedName) void selectProfile(selectedName);
        }}
      />

      <div className="split-layout">
        <aside className="split-list">
          <div className="btn-row">
            <button type="button" onClick={startNew}>
              Add profile
            </button>
            <button type="button" onClick={() => void loadList()}>
              Refresh list
            </button>
          </div>
          <ul className="item-list">
            {list.map((p) => (
              <li key={p.name}>
                <button
                  type="button"
                  className={selectedName === p.name && !isNew ? "list-active" : ""}
                  onClick={() => void selectProfile(p.name)}
                >
                  <strong>{p.name}</strong>
                  {p.auto_apply ? <span className="pill">auto_apply</span> : null}
                </button>
              </li>
            ))}
            {list.length === 0 ? <li className="muted-text">No profiles</li> : null}
          </ul>
        </aside>

        <section className="split-editor">
          <h2>{isNew ? "New profile" : selectedName ? `Edit ${selectedName}` : "Select a profile"}</h2>
          {(isNew || selectedName) && (
            <>
              <div className="form-grid">
                <label>
                  <span className="field-label-row">
                    Name
                    <FieldHelp label="Name" id="ph-name" text={PROFILE_HELP.name} />
                  </span>
                  <input
                    value={form.name}
                    disabled={!isNew}
                    aria-describedby="ph-name"
                    onChange={(e) => patch({ name: e.target.value })}
                  />
                </label>
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={Boolean(form.auto_apply)}
                    aria-describedby="ph-autoapply"
                    onChange={(e) => patch({ auto_apply: e.target.checked })}
                  />
                  Auto-apply at session init
                  <FieldHelp label="Auto-apply" id="ph-autoapply" text={PROFILE_HELP.autoApply} />
                </label>
                <label className="span-2">
                  <span className="field-label-row">
                    Description
                    <FieldHelp label="Description" id="ph-desc" text={PROFILE_HELP.description} />
                  </span>
                  <input
                    value={form.description ?? ""}
                    aria-describedby="ph-desc"
                    onChange={(e) => patch({ description: e.target.value })}
                  />
                </label>
              </div>

              <h3 className="section-heading">Selectors</h3>
              {(form.selectors ?? []).map((sel, i) => (
                <div key={i} className="selector-block">
                  <div className="form-grid">
                    <label>
                      <span className="field-label-row">
                        Server
                        <FieldHelp label="Server" id={`ph-${i}-server`} text={PROFILE_HELP.server} />
                      </span>
                      <input
                        value={sel.server ?? ""}
                        aria-describedby={`ph-${i}-server`}
                        onChange={(e) => updateSelector(i, { server: e.target.value || null })}
                      />
                    </label>
                    <label>
                      <span className="field-label-row">
                        Primitive type
                        <FieldHelp
                          label="Primitive type"
                          id={`ph-${i}-ptype`}
                          text={PROFILE_HELP.primitiveType}
                        />
                      </span>
                      <select
                        value={sel.primitive_type ?? ""}
                        aria-describedby={`ph-${i}-ptype`}
                        onChange={(e) =>
                          updateSelector(i, {
                            primitive_type: (e.target.value || null) as ProfileSelector["primitive_type"],
                          })
                        }
                      >
                        {PRIMITIVE_TYPES.map((t) => (
                          <option key={t || "any"} value={t}>
                            {t || "any"}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="span-2">
                      <span className="field-label-row">
                        Tags (comma-separated)
                        <FieldHelp label="Tags" id={`ph-${i}-tags`} text={PROFILE_HELP.tags} />
                      </span>
                      <input
                        value={(sel.tags ?? []).join(", ")}
                        aria-describedby={`ph-${i}-tags`}
                        onChange={(e) => updateSelector(i, { tags: csvToList(e.target.value) })}
                      />
                    </label>
                    <label className="span-2">
                      <span className="field-label-row">
                        Categories (comma-separated)
                        <FieldHelp
                          label="Categories"
                          id={`ph-${i}-cats`}
                          text={PROFILE_HELP.categories}
                        />
                      </span>
                      <input
                        value={(sel.categories ?? []).join(", ")}
                        aria-describedby={`ph-${i}-cats`}
                        onChange={(e) => updateSelector(i, { categories: csvToList(e.target.value) })}
                      />
                    </label>
                    <label className="span-2">
                      <span className="field-label-row">
                        Names (comma-separated; canonical form e.g. server__tool, or upstream name e.g. tool)
                        <FieldHelp label="Names" id={`ph-${i}-names`} text={PROFILE_HELP.names} />
                      </span>
                      <input
                        value={(sel.names ?? []).join(", ")}
                        aria-describedby={`ph-${i}-names`}
                        onChange={(e) => updateSelector(i, { names: csvToList(e.target.value) })}
                      />
                    </label>
                  </div>
                  <button type="button" className="btn-danger" onClick={() => removeSelector(i)}>
                    Remove selector
                  </button>
                </div>
              ))}
              <button type="button" onClick={addSelector}>
                Add selector
              </button>

              <div className="btn-row">
                <button type="button" onClick={() => void save()} disabled={busy}>
                  Save to draft
                </button>
                <button type="button" onClick={() => void runPreview()} disabled={busy}>
                  Preview
                </button>
                {!isNew ? (
                  <>
                    <button type="button" onClick={() => void duplicate()} disabled={busy}>
                      Duplicate
                    </button>
                    <button type="button" className="btn-danger" onClick={() => void remove()} disabled={busy}>
                      Delete
                    </button>
                  </>
                ) : null}
              </div>

              {preview ? (
                <article className="card card-wide">
                  <h2>Preview: {preview.profile}</h2>
                  <p>
                    Matched <strong>{preview.matched_count}</strong> primitive
                    {preview.matched_count === 1 ? "" : "s"}
                  </p>
                  {preview.warnings.length > 0 ? (
                    <ul className="warn-list">
                      {preview.warnings.map((w) => (
                        <li key={w}>{w}</li>
                      ))}
                    </ul>
                  ) : null}
                  {preview.matched_count === 0 ? (
                    <p className="bad-text">No catalog entries matched — check selectors.</p>
                  ) : (
                    <>
                      <div className="preview-table-wrap">
                        <table className="server-table preview-table">
                          <thead>
                            <tr>
                              <th>Name</th>
                              <th>Server</th>
                              <th>Kind</th>
                            </tr>
                          </thead>
                          <tbody>
                            {preview.primitives.map((e) => (
                              <tr key={primitiveDisplayName(e)}>
                                <td>{primitiveDisplayName(e)}</td>
                                <td>{e.server_id ?? "—"}</td>
                                <td>{e.primitive_type ?? e.kind ?? "—"}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                      {preview.primitives.length < preview.canonical_names.length ? (
                        <p className="muted-text">
                          Showing {preview.primitives.length} of {preview.canonical_names.length}{" "}
                          matched (server-side cap reached — narrow selectors to see the rest).
                        </p>
                      ) : (
                        <p className="muted-text">Showing all {preview.primitives.length} matched primitives.</p>
                      )}
                    </>
                  )}
                </article>
              ) : null}
            </>
          )}
        </section>
      </div>
    </div>
  );
}