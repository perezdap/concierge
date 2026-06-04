/**
 * Pending config draft + apply / rollback affordance for upstream/profile pages.
 */
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../src/api";

export interface PendingChangesProps {
  applyDraft: () => Promise<{ version_id: string; applied: boolean; reloaded: boolean }>;
  onApplied?: () => void;
}

export function PendingChanges({ applyDraft, onApplied }: PendingChangesProps) {
  const [draftId, setDraftId] = useState<string | null>(null);
  const [draftStatus, setDraftStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.configDraft();
      setDraftId(res.version?.id ?? null);
      setDraftStatus(res.version?.status ?? null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Load failed");
      setDraftId(null);
      setDraftStatus(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const runApply = async () => {
    setBusy(true);
    setActionMsg(null);
    setError(null);
    try {
      const res = await applyDraft();
      setActionMsg(
        `Applied draft ${res.version_id}${res.reloaded ? " (runtime reloaded)" : ""}.`,
      );
      await load();
      onApplied?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Apply failed");
    } finally {
      setBusy(false);
    }
  };

  const runRollback = async () => {
    if (!window.confirm("Roll back active config to the previous version?")) {
      return;
    }
    setBusy(true);
    setActionMsg(null);
    setError(null);
    try {
      const res = await api.configRollback();
      setActionMsg(`Rolled back to version ${(res.version as { id?: string })?.id ?? "prior"}.`);
      await load();
      onApplied?.();
    } catch (e) {
      setError(
        e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Rollback failed",
      );
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return <p className="muted-text">Loading draft…</p>;
  }

  return (
    <article className="card card-wide">
      <h2>Pending changes</h2>
      {error ? <div className="banner error">{error}</div> : null}
      {actionMsg ? <div className="banner">{actionMsg}</div> : null}
      {!draftId ? (
        <p className="muted-text">No pending draft — edits create a draft automatically.</p>
      ) : (
        <p>
          Draft <code>{draftId}</code>
          {draftStatus ? ` (${draftStatus})` : ""}
        </p>
      )}
      <div className="btn-row">
        <button type="button" onClick={() => void load()} disabled={busy}>
          Refresh
        </button>
        <button type="button" onClick={() => void runApply()} disabled={busy || !draftId}>
          Apply (no restart)
        </button>
        <button type="button" onClick={() => void runRollback()} disabled={busy}>
          Rollback config
        </button>
      </div>
    </article>
  );
}