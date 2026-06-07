/**
 * Shared HTTP client for Concierge admin + observability endpoints.
 * Imported by Dashboard and future pages (Upstreams, Profiles).
 */

import { ensureBearerToken, getAuthHeaders, getAuthMode, setAuthMode } from "./auth";

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (!ensureBearerToken()) {
    throw new ApiError("Admin bearer token required", 401, null);
  }
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...getAuthHeaders(),
    ...(init?.headers as Record<string, string> | undefined),
  };
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  let body: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text) as unknown;
    } catch {
      body = text;
    }
  }
  if (!res.ok) {
    if (res.status === 401 && getAuthMode() === "localhost") {
      setAuthMode("bearer");
      if (ensureBearerToken()) {
        return request<T>(path, init);
      }
    }
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : res.statusText;
    throw new ApiError(detail || `HTTP ${res.status}`, res.status, body);
  }
  return body as T;
}

/** Like ``request`` but returns parsed JSON for listed non-2xx statuses (e.g. /readyz 503). */
async function requestAllowStatuses<T>(
  path: string,
  allowedStatuses: number[],
  init?: RequestInit,
): Promise<T> {
  if (!ensureBearerToken()) {
    throw new ApiError("Admin bearer token required", 401, null);
  }
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...getAuthHeaders(),
    ...(init?.headers as Record<string, string> | undefined),
  };
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  let body: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text) as unknown;
    } catch {
      body = text;
    }
  }
  if (!res.ok && !allowedStatuses.includes(res.status)) {
    if (res.status === 401 && getAuthMode() === "localhost") {
      setAuthMode("bearer");
      if (ensureBearerToken()) {
        return requestAllowStatuses<T>(path, allowedStatuses, init);
      }
    }
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : res.statusText;
    throw new ApiError(detail || `HTTP ${res.status}`, res.status, body);
  }
  return body as T;
}

export interface AdapterHealth {
  server_id?: string;
  transport?: string;
  connected?: boolean;
  last_error?: string | null;
  circuit_open?: boolean;
  circuit_open_until?: string | null;
}

export interface AdminHealth {
  ok: boolean;
  catalog_count: number;
  session_count: number;
  servers: AdapterHealth[];
}

export interface ReadyzResponse {
  ok: boolean;
  draining?: boolean;
  upstreams?: AdapterHealth[];
}

export interface CatalogEntry {
  name: string;
  canonical_name?: string;
  server_id?: string;
  primitive_type?: string;
  kind?: string;
}

export interface ConfigStatus {
  config_store: boolean;
  active_version_id: string | null;
  draft_version_id: string | null;
  draft_status: string | null;
  reload_coordinator: boolean;
  catalog_count: number;
  session_count: number;
  servers: AdapterHealth[];
}

export interface SessionRecord {
  session_id: string;
  tenant_id?: string;
  created_at?: string;
}

export type UpstreamTransport = "stdio" | "streamable_http" | "sse_legacy" | "custom";

export interface UpstreamRecord {
  id: string;
  transport: UpstreamTransport;
  command?: string[] | null;
  env?: Record<string, string> | null;
  cwd?: string | null;
  url?: string | null;
  sse_url?: string | null;
  post_url?: string | null;
  headers?: Record<string, string>;
  custom_kind?: string | null;
  custom_params?: Record<string, unknown>;
  default_risk?: string;
  default_tags?: string[];
  default_categories?: string[];
  isolation?: string;
  request_timeout_s?: number;
}

export interface TestConnectionResult {
  ok: boolean;
  server_id: string;
  transport: string;
  connected?: boolean;
  tools_discovered?: number;
  resources_discovered?: number;
  prompts_discovered?: number;
  latency_ms?: number | null;
  error?: string | null;
}

export interface OAuthProviderInfo {
  id: string;
  display_name: string;
  default_scopes: string;
  ready: boolean;
  env_client_id: string;
  env_client_secret: string;
}

export interface OAuthStatus {
  upstream_id: string;
  connected: boolean;
  credential?: Record<string, unknown>;
}

export interface SignInStartResult {
  authorization_url: string;
  state: string;
  redirect_uri: string;
}

export interface SignInStartRequest {
  resource_url?: string;
  provider?: string;
  issuer?: string;
  client_id?: string;
  scopes?: string;
  client_secret?: string;
  redirect_uri?: string;
}

export interface ProbeResult {
  supports_dcr: boolean;
  authorization_server: string | null;
  protected: boolean;
}

export interface ProfileSelector {
  server?: string | null;
  tags?: string[];
  categories?: string[];
  primitive_type?: "tool" | "resource" | "prompt" | null;
  names?: string[];
}

export interface ProfileRecord {
  name: string;
  description?: string;
  selectors?: ProfileSelector[];
  auto_apply?: boolean;
}

export interface ProfilePreviewResult {
  profile: string;
  matched_count: number;
  canonical_names: string[];
  primitives: CatalogEntry[];
  warnings: string[];
}

function postJson<T>(path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method: "POST" };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  return request<T>(path, init);
}

function putJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export const api = {
  healthz: () => request<{ ok: boolean }>("/healthz"),
  readyz: () => requestAllowStatuses<ReadyzResponse>("/readyz", [503]),
  adminHealth: () => request<AdminHealth>("/admin/health"),
  catalog: () => request<{ entries: CatalogEntry[] }>("/admin/catalog"),
  sessions: () => request<{ sessions: SessionRecord[] }>("/admin/sessions"),
  configStatus: () => request<ConfigStatus>("/admin/config/status"),
  configDraft: () =>
    request<{
      config: Record<string, unknown> | null;
      version: {
        id: string;
        status: string;
        created_at?: string;
        created_by?: string;
      } | null;
    }>("/admin/config/draft"),
  configRollback: () => postJson<{ version: unknown; rolled_back: boolean }>("/admin/config/rollback"),

  listUpstreams: () =>
    request<{ upstreams: UpstreamRecord[]; source: string }>("/admin/upstreams"),
  getUpstream: (id: string) => request<{ upstream: UpstreamRecord }>(`/admin/upstreams/${id}`),
  createUpstream: (upstream: UpstreamRecord) =>
    postJson<{ upstream: UpstreamRecord; version_id: string }>("/admin/upstreams", { upstream }),
  updateUpstream: (id: string, upstream: UpstreamRecord) =>
    putJson<{ upstream: UpstreamRecord; version_id: string }>(`/admin/upstreams/${id}`, {
      upstream,
    }),
  deleteUpstream: (id: string) =>
    request<{ removed_id: string; version_id: string }>(`/admin/upstreams/${id}`, {
      method: "DELETE",
    }),
  testUpstreamConnection: (id: string, upstream?: UpstreamRecord) =>
    postJson<TestConnectionResult>(`/admin/upstreams/${id}/test-connection`, upstream ? { upstream } : undefined),
  refreshUpstreamCatalog: (id: string) =>
    postJson<{ server: string; entries: number }>(`/admin/upstreams/${id}/refresh`),
  applyUpstreamsDraft: () =>
    postJson<{ version_id: string; applied: boolean; reloaded: boolean }>("/admin/upstreams/apply"),

  listProfiles: () =>
    request<{ profiles: ProfileRecord[]; source: string }>("/admin/profiles"),
  getProfile: (name: string) => request<{ profile: ProfileRecord }>(`/admin/profiles/${encodeURIComponent(name)}`),
  createProfile: (profile: ProfileRecord) =>
    postJson<{ profile: ProfileRecord; version_id: string }>("/admin/profiles", { profile }),
  updateProfile: (name: string, profile: ProfileRecord) =>
    putJson<{ profile: ProfileRecord; version_id: string }>(`/admin/profiles/${encodeURIComponent(name)}`, {
      profile,
    }),
  deleteProfile: (name: string) =>
    request<{ removed_name: string; version_id: string }>(
      `/admin/profiles/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  duplicateProfile: (name: string, newName: string) =>
    postJson<{ profile: ProfileRecord; version_id: string }>(
      `/admin/profiles/${encodeURIComponent(name)}/duplicate`,
      { new_name: newName },
    ),
  previewProfile: (name: string, profile?: ProfileRecord) =>
    postJson<ProfilePreviewResult>(
      `/admin/profiles/${encodeURIComponent(name)}/preview`,
      profile ? { profile } : undefined,
    ),
  applyProfilesDraft: () =>
    postJson<{ version_id: string; applied: boolean; reloaded: boolean }>("/admin/profiles/apply"),

  // --- Upstream OAuth (outbound) ---
  listOAuthProviders: () =>
    request<{ providers: OAuthProviderInfo[] }>("/admin/oauth/providers"),
  oauthStatus: (id: string) =>
    request<OAuthStatus>(`/admin/oauth/${encodeURIComponent(id)}/status`),
  oauthProbe: (id: string, resourceUrl: string) =>
    postJson<ProbeResult>(`/admin/oauth/${encodeURIComponent(id)}/probe`, {
      resource_url: resourceUrl,
    }),
  oauthSignInStart: (id: string, body: SignInStartRequest) =>
    postJson<SignInStartResult>(`/admin/oauth/${encodeURIComponent(id)}/sign-in/start`, body),
  oauthDisconnect: (id: string) =>
    request<{ status: string; upstream_id: string }>(`/admin/oauth/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
};