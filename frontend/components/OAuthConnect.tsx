import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  type OAuthProviderInfo,
  type OAuthStatus,
  type SignInStartRequest,
} from "../src/api";

/**
 * One-click OAuth connection for an HTTP/SSE upstream.
 *
 * End users pick a provider and click Connect; the browser-login flow opens in a
 * popup and the credential is stored + auto-refreshed by the backend. When the
 * operator has configured app credentials (env vars) the provider is "ready" and
 * needs no further input; otherwise we reveal client id/secret fields for that
 * provider only (bring-your-own fallback).
 */
export function OAuthConnect({ upstreamId }: { upstreamId: string }) {
  const [providers, setProviders] = useState<OAuthProviderInfo[]>([]);
  const [status, setStatus] = useState<OAuthStatus | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const provider = providers.find((p) => p.id === selected) ?? null;
  const needsCreds = provider != null && !provider.ready;

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.oauthStatus(upstreamId));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Status check failed");
    }
  }, [upstreamId]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await api.listOAuthProviders();
        if (cancelled) return;
        setProviders(res.providers);
        setSelected((cur) => cur || res.providers[0]?.id || "");
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof ApiError ? e.message : "Could not load providers");
        }
      }
    })();
    void loadStatus();
    return () => {
      cancelled = true;
    };
  }, [loadStatus]);

  const connect = async () => {
    if (!provider) return;
    if (needsCreds && (!clientId.trim() || !clientSecret.trim())) {
      setError("Enter the client ID and secret for this provider");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const body: SignInStartRequest = { provider: provider.id };
      if (needsCreds) {
        body.client_id = clientId.trim();
        body.client_secret = clientSecret.trim();
      }
      const res = await api.oauthSignInStart(upstreamId, body);
      const popup = window.open(res.authorization_url, "concierge-oauth", "width=600,height=720");
      // Resolve when the popup signals completion or is closed by the user.
      await new Promise<void>((resolve) => {
        const onMessage = (ev: MessageEvent) => {
          if (ev.data && (ev.data as { type?: string }).type === "concierge:oauth") {
            cleanup();
            resolve();
          }
        };
        const timer = window.setInterval(() => {
          if (popup?.closed) {
            cleanup();
            resolve();
          }
        }, 600);
        const cleanup = () => {
          window.clearInterval(timer);
          window.removeEventListener("message", onMessage);
        };
        window.addEventListener("message", onMessage);
      });
      await loadStatus();
      setClientSecret("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Connect failed");
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    if (!window.confirm(`Disconnect OAuth for "${upstreamId}"?`)) return;
    setBusy(true);
    setError(null);
    try {
      await api.oauthDisconnect(upstreamId);
      await loadStatus();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Disconnect failed");
    } finally {
      setBusy(false);
    }
  };

  const connected = status?.connected === true;

  return (
    <article className="card oauth-connect">
      <h2>Authentication</h2>
      {connected ? (
        <p className="ok-text">✅ Connected — tokens are injected and auto-refreshed.</p>
      ) : (
        <p className="muted-text">
          Connect this upstream with OAuth so requests carry a token automatically.
        </p>
      )}

      {error ? <p className="bad-text">{error}</p> : null}

      <div className="form-grid">
        <label>
          Provider
          <select value={selected} onChange={(e) => setSelected(e.target.value)} disabled={busy}>
            {providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.display_name}
                {p.ready ? "" : " (setup required)"}
              </option>
            ))}
          </select>
        </label>

        {needsCreds ? (
          <>
            <label>
              Client ID
              <input value={clientId} onChange={(e) => setClientId(e.target.value)} disabled={busy} />
            </label>
            <label>
              Client secret
              <input
                type="password"
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
                disabled={busy}
              />
            </label>
            <p className="hint-text span-2">
              Tip: set <code>{provider?.env_client_id}</code> and{" "}
              <code>{provider?.env_client_secret}</code> on the server to make this
              one-click for everyone.
            </p>
          </>
        ) : null}
      </div>

      <div className="btn-row">
        <button type="button" onClick={() => void connect()} disabled={busy || !provider}>
          {connected ? "Reconnect" : "Connect"}
        </button>
        {connected ? (
          <button type="button" className="btn-danger" onClick={() => void disconnect()} disabled={busy}>
            Disconnect
          </button>
        ) : null}
      </div>
    </article>
  );
}
