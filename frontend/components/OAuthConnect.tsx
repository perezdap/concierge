import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  type OAuthProviderInfo,
  type OAuthStatus,
  type ProbeResult,
  type SignInStartRequest,
} from "../src/api";

/**
 * OAuth connection for an HTTP/SSE upstream, DCR-first.
 *
 * Resolution, in order of friendliness:
 *  1. Zero-config (DCR): if the upstream is a spec-compliant MCP server that
 *     advertises an OAuth authorization server supporting Dynamic Client
 *     Registration (RFC 9728 + RFC 7591), the user just clicks Connect — no
 *     provider, secret, or operator setup. This is the Docker-toolkit-like path.
 *  2. Provider preset: pick GitHub/Google/etc.; one-click when the operator has
 *     configured app credentials, otherwise paste client id/secret.
 *
 * On mount we probe the upstream to decide which path to show.
 */
export function OAuthConnect({
  upstreamId,
  resourceUrl,
}: {
  upstreamId: string;
  resourceUrl: string | null;
}) {
  const [providers, setProviders] = useState<OAuthProviderInfo[]>([]);
  const [status, setStatus] = useState<OAuthStatus | null>(null);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [probing, setProbing] = useState(true);
  const [mode, setMode] = useState<"auto" | "provider">("auto");
  const [selected, setSelected] = useState<string>("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const provider = providers.find((p) => p.id === selected) ?? null;
  const needsCreds = provider != null && !provider.ready;
  const canDcr = probe?.supports_dcr === true;

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
      // Probe for zero-config support first; fall back to provider presets.
      setProbing(true);
      if (resourceUrl) {
        try {
          const res = await api.oauthProbe(upstreamId, resourceUrl);
          if (!cancelled) {
            setProbe(res);
            setMode(res.supports_dcr ? "auto" : "provider");
          }
        } catch {
          if (!cancelled) setMode("provider");
        }
      } else if (!cancelled) {
        setMode("provider");
      }
      if (!cancelled) setProbing(false);

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
  }, [upstreamId, resourceUrl, loadStatus]);

  const runPopup = async (body: SignInStartRequest) => {
    const res = await api.oauthSignInStart(upstreamId, body);
    const popup = window.open(res.authorization_url, "concierge-oauth", "width=600,height=720");
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
  };

  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      if (mode === "auto" && canDcr && resourceUrl) {
        // Zero-config: backend probes + discovers + registers a client.
        await runPopup({ resource_url: resourceUrl });
      } else {
        if (!provider) {
          setError("Pick a provider to connect");
          return;
        }
        if (needsCreds && (!clientId.trim() || !clientSecret.trim())) {
          setError("Enter the client ID and secret for this provider");
          return;
        }
        const body: SignInStartRequest = { provider: provider.id };
        if (needsCreds) {
          body.client_id = clientId.trim();
          body.client_secret = clientSecret.trim();
        }
        await runPopup(body);
        setClientSecret("");
      }
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
  const showProviderPicker = mode === "provider";

  return (
    <article className="card oauth-connect">
      <h2>Authentication</h2>
      {connected ? (
        <p className="ok-text">✅ Connected — tokens are injected and auto-refreshed.</p>
      ) : probing ? (
        <p className="muted-text">Checking how this upstream wants to authenticate…</p>
      ) : canDcr && mode === "auto" ? (
        <p className="muted-text">
          This upstream supports one-click sign-in. Click <strong>Connect</strong> — no
          setup, no secrets.
        </p>
      ) : (
        <p className="muted-text">
          Connect this upstream with OAuth so requests carry a token automatically.
        </p>
      )}

      {error ? <p className="bad-text">{error}</p> : null}

      {showProviderPicker ? (
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
      ) : null}

      <div className="btn-row">
        <button type="button" onClick={() => void connect()} disabled={busy || probing}>
          {connected ? "Reconnect" : "Connect"}
        </button>
        {connected ? (
          <button type="button" className="btn-danger" onClick={() => void disconnect()} disabled={busy}>
            Disconnect
          </button>
        ) : null}
        {canDcr && mode === "auto" ? (
          <button type="button" className="btn-link" onClick={() => setMode("provider")} disabled={busy}>
            Use a specific provider instead
          </button>
        ) : null}
        {mode === "provider" && canDcr ? (
          <button type="button" className="btn-link" onClick={() => setMode("auto")} disabled={busy}>
            Back to one-click
          </button>
        ) : null}
      </div>
    </article>
  );
}
