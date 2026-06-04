import { NavLink, Outlet, Route, Routes } from "react-router-dom";
import { useState } from "react";
import {
  authLabel,
  clearBearerToken,
  getAuthMode,
  setAuthMode,
  setBearerToken,
  type AuthMode,
} from "./auth";
import Dashboard from "./pages/Dashboard";
import Profiles from "./pages/Profiles";
import Upstreams from "./pages/Upstreams";

function Shell() {
  const [authMode, setMode] = useState<AuthMode>(getAuthMode());
  const [authHint, setAuthHint] = useState(authLabel());

  const onModeChange = (mode: AuthMode) => {
    setAuthMode(mode);
    setMode(mode);
    setAuthHint(authLabel());
  };

  const onSetToken = () => {
    const token = window.prompt("Bearer token:");
    if (token?.trim()) {
      setBearerToken(token);
      setMode("bearer");
      setAuthHint(authLabel());
    }
  };

  const onClearToken = () => {
    clearBearerToken();
    setAuthHint(authLabel());
  };

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-brand">
          <span className="app-brand-mark">C</span>
          <span>Concierge Admin</span>
        </div>
        <nav className="app-nav">
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : undefined)}>
            Dashboard
          </NavLink>
          <NavLink to="/upstreams" className={({ isActive }) => (isActive ? "active" : undefined)}>
            Upstreams
          </NavLink>
          <NavLink to="/profiles" className={({ isActive }) => (isActive ? "active" : undefined)}>
            Profiles
          </NavLink>
        </nav>
        <div className="auth-bar">
          <select
            value={authMode}
            onChange={(e) => onModeChange(e.target.value as AuthMode)}
            aria-label="Auth mode"
          >
            <option value="localhost">localhost</option>
            <option value="bearer">bearer</option>
          </select>
          <span>{authHint}</span>
          {authMode === "bearer" ? (
            <>
              <button type="button" onClick={onSetToken}>
                Set token
              </button>
              <button type="button" onClick={onClearToken}>
                Clear
              </button>
            </>
          ) : null}
        </div>
      </header>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Dashboard />} />
        <Route path="upstreams" element={<Upstreams />} />
        <Route path="profiles" element={<Profiles />} />
      </Route>
    </Routes>
  );
}