import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import federation from "@originjs/vite-plugin-federation";

// The first-party Notes plugin REMOTE (ADR 0034 S4). Built into the host's public/ so it's served
// same-origin at /app/remotes/notes/remoteEntry.js; the plugin manifest points its ui:react view
// here. Shares the host's React/query + the @protoagent/plugin-ui SDK (the host bridge).
const here = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: here,
  base: "/app/remotes/notes/",
  plugins: [
    react(),
    federation({
      name: "notes_panel",
      filename: "remoteEntry.js",
      exposes: { "./Panel": `${here}/Panel.tsx` },
      shared: {
        react: { requiredVersion: false },
        "react-dom": { requiredVersion: false },
        "@tanstack/react-query": { requiredVersion: false },
        "@protoagent/plugin-ui": { requiredVersion: false },
      },
    }),
  ],
  build: { target: "esnext", outDir: "../../public/remotes/notes", emptyOutDir: true },
});
