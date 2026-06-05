import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = process.env.VITE_DEV_BACKEND ?? "http://127.0.0.1:8765";

/** Let Vite serve the SPA; proxy JSON admin/observability API to Concierge. */
function apiProxyBypass(req: { url?: string; headers?: { accept?: string } }) {
  const accept = req.headers?.accept ?? "";
  if (accept.includes("text/html")) {
    return req.url;
  }
  return undefined;
}

export default defineConfig({
  plugins: [react()],
  base: "/admin/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/healthz": { target: backend, changeOrigin: true },
      "/readyz": { target: backend, changeOrigin: true },
      "/metrics": { target: backend, changeOrigin: true },
      "/admin": {
        target: backend,
        changeOrigin: true,
        bypass: apiProxyBypass,
      },
    },
  },
});