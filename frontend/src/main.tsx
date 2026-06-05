import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { detectDefaultAuthMode, setAuthMode } from "./auth";
import App from "./App";
import "./styles.css";

if (!sessionStorage.getItem("concierge.admin.auth_mode")) {
  setAuthMode(detectDefaultAuthMode());
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter basename="/admin">
      <App />
    </BrowserRouter>
  </StrictMode>,
);