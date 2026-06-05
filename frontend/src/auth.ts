/** Admin console auth: localhost (no token) vs bearer token mode. */

export type AuthMode = "localhost" | "bearer";

const TOKEN_KEY = "concierge.admin.bearer_token";
const MODE_KEY = "concierge.admin.auth_mode";

function isLoopbackHost(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]";
}

export function detectDefaultAuthMode(): AuthMode {
  if (typeof window === "undefined") {
    return "bearer";
  }
  return isLoopbackHost(window.location.hostname) ? "localhost" : "bearer";
}

export function getAuthMode(): AuthMode {
  const stored = sessionStorage.getItem(MODE_KEY);
  if (stored === "localhost" || stored === "bearer") {
    return stored;
  }
  return detectDefaultAuthMode();
}

export function setAuthMode(mode: AuthMode): void {
  sessionStorage.setItem(MODE_KEY, mode);
  if (mode === "localhost") {
    sessionStorage.removeItem(TOKEN_KEY);
  }
}

export function getBearerToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setBearerToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token.trim());
  sessionStorage.setItem(MODE_KEY, "bearer");
}

export function clearBearerToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

/** Ensure bearer mode has a token; prompts once per session when missing. */
export function ensureBearerToken(): boolean {
  if (getAuthMode() === "localhost") {
    return true;
  }
  if (getBearerToken()) {
    return true;
  }
  const entered = window.prompt(
    "Enter admin bearer token (stored for this browser session only):",
  );
  if (!entered?.trim()) {
    return false;
  }
  setBearerToken(entered);
  return true;
}

export function getAuthHeaders(): Record<string, string> {
  if (getAuthMode() === "localhost") {
    return {};
  }
  const token = getBearerToken();
  if (!token) {
    return {};
  }
  return { Authorization: `Bearer ${token}` };
}

export function authLabel(): string {
  if (getAuthMode() === "localhost") {
    return "localhost (no token)";
  }
  const token = getBearerToken();
  return token ? "bearer token set" : "bearer (token missing)";
}