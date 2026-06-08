import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import federation from "@originjs/vite-plugin-federation";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, new URL("../..", import.meta.url).pathname, "PROTOAGENT_");
  const apiBase = env.PROTOAGENT_API_BASE || "http://127.0.0.1:7870";

  return {
    base: "/app/",
    plugins: [
      react(),
      // Plugin UI as first-class React (ADR 0034). The console is the Module Federation
      // *host*: it shares React + react-query as singletons so a `ui: react` plugin remote
      // mounts into this tree with ONE React instance + ONE query cache. Remotes load
      // dynamically at runtime (URL from the plugin manifest) via the federation runtime
      // helpers in FederatedView — so none are declared statically here.
      federation({
        name: "console_host",
        // A placeholder remote forces vite-plugin-federation to emit the shared-scope runtime
        // (without ≥1 remote, the dynamic setRemote/getRemote path throws "__rf_placeholder__
        // shareScope is not defined"). It's never imported — real remotes are registered at
        // runtime from the plugin manifest in FederatedView.
        remotes: { __pa_share_init__: "data:text/javascript,export default {}" },
        // vite-plugin-federation shares by provision (no Webpack-style `singleton` flag — it's
        // commented out in its types). The host provides these; a remote consumes the host's
        // copy, so there's one React + one query cache. `requiredVersion: false` stops a
        // version-string mismatch (host React 19 vs a remote's declared range) from dual-loading.
        shared: {
          react: { requiredVersion: false },
          "react-dom": { requiredVersion: false },
          "@tanstack/react-query": { requiredVersion: false },
        },
      }),
    ],
    build: {
      // Federation's shared-scope bootstrap emits modern output (top-level await).
      target: "esnext",
    },
    server: {
      port: 5173,
      proxy: {
        "/api": apiBase,
        "/a2a": apiBase,
        "/v1": apiBase,
      },
    },
  };
});
