import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, new URL("../..", import.meta.url).pathname, "PROTOAGENT_");
  // The dev/preview server proxies the console's backend calls to `apiBase`. It DEFAULTS to the
  // ISOLATED dev instance (:7871, what `scripts/dev.sh` runs) — deliberately NOT the default/prod
  // instance (:7870, what the desktop app runs). Rationale: `npm run dev`/`preview` must never
  // silently read/write your real ~/.protoagent data. If no dev backend is up on :7871 you get a
  // clean "can't connect" (fail-safe) instead of a silent prod hit. Override with PROTOAGENT_API_BASE.
  const apiBase = env.PROTOAGENT_API_BASE || "http://127.0.0.1:7871";

  // Loud guard: proxying the dev frontend at :7870 points it straight at the default/prod
  // (desktop-app) instance — every console action would hit your real data. Scream about it.
  if (apiBase.indexOf(":7870") !== -1) {
    const bar = Array(77).join("═"); // ES5-safe (tsconfig.node lib predates String.repeat)
    console.warn(
      `\n\x1b[41m\x1b[97m\x1b[1m ${bar} \x1b[0m` +
        `\n\x1b[41m\x1b[97m\x1b[1m  ⚠  DEV FRONTEND → ${apiBase} — the PROD / desktop-app backend (:7870).           \x1b[0m` +
        `\n\x1b[41m\x1b[97m\x1b[1m     Console actions (chat, goals, /compact…) will read/WRITE your real          \x1b[0m` +
        `\n\x1b[41m\x1b[97m\x1b[1m     ~/.protoagent data. Use an ISOLATED dev backend instead:                    \x1b[0m` +
        `\n\x1b[41m\x1b[97m\x1b[1m       scripts/dev.sh          # isolated instance on :7871                       \x1b[0m` +
        `\n\x1b[41m\x1b[97m\x1b[1m       unset PROTOAGENT_API_BASE (defaults to :7871), or set it to :7871.         \x1b[0m` +
        `\n\x1b[41m\x1b[97m\x1b[1m ${bar} \x1b[0m\n`,
    );
  }

  // Module Federation was retired (ADR 0038): plugin UI is sandboxed iframes (untrusted /
  // generative) + the build-time fork seam (trusted) — no runtime remote loading.
  const proxy = {
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
  };
  return {
    base: "/app/",
    plugins: [react()],
    // `preview` serves the rollup build (apps/web/dist) — same proxy as the HMR dev server, but
    // it avoids the esbuild dev dep-optimization (a CJS-interop edge with style-to-js), so it's
    // the reliable way to eyeball a built change against a running backend.
    server: { port: 5173, proxy },
    preview: { port: 5173, proxy },
  };
});
