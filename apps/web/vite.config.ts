import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, new URL("../..", import.meta.url).pathname, "PROTOAGENT_");
  const apiBase = env.PROTOAGENT_API_BASE || "http://127.0.0.1:7870";

  // Module Federation was retired (ADR 0038): plugin UI is sandboxed iframes (untrusted /
  // generative) + the build-time fork seam (trusted) — no runtime remote loading.
  return {
    base: "/app/",
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": apiBase,
        "/a2a": apiBase,
        "/v1": apiBase,
        // The fleet hub's reverse proxy to the focused agent (ADR 0042). When a peer is
        // active, the console rewrites agent calls to /active/* (XHR AND plugin-view iframe
        // srcs). In prod the backend serves /active; the dev server must proxy it too, else
        // every switched-into call (and iframe) 404s on Vite's /app/ base.
        "/active": apiBase,
        // Plugin-contributed views are iframes the backend serves at /plugins/<id>/…
        // (ADR 0026). In prod the backend serves /app + /plugins together; the dev
        // server must proxy them too, else a plugin view 404s on Vite's /app/ base.
        "/plugins": apiBase,
        "/healthz": apiBase,
      },
    },
  };
});
