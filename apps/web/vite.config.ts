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
        // The fleet hub's per-agent reverse proxy (ADR 0042 slug routing). A console window on
        // /app/agent/<slug>/ rewrites agent calls to /agents/<slug>/* (XHR AND plugin-view iframe
        // srcs). In prod the backend serves /agents; the dev server must proxy it too, else every
        // call (and iframe) from a peer window 404s on Vite's /app/ base.
        "/agents": apiBase,
        // Plugin-contributed views are iframes the backend serves at /plugins/<id>/…
        // (ADR 0026). In prod the backend serves /app + /plugins together; the dev
        // server must proxy them too, else a plugin view 404s on Vite's /app/ base.
        "/plugins": apiBase,
        // The DS plugin-kit (CSS + JS) the backend serves same-origin at /_ds/… A plugin
        // iframe base-splits to "" and requests root-absolute /_ds/plugin-kit.{css,js}; it
        // bypasses Vite's /app/ base, so without this proxy it 404s on the dev server and
        // EVERY plugin view loses its theme handshake (prod is fine — the backend serves
        // /_ds from the built dist). Mirrors /plugins above.
        "/_ds": apiBase,
        "/healthz": apiBase,
      },
    },
  };
});
