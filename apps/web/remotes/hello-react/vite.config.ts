import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import federation from "@originjs/vite-plugin-federation";

// A trivial first-party React plugin REMOTE (ADR 0034 slice 1) — proves a federated
// component mounts into the console and shares the host's React. `root` is pinned to this
// dir so the build stays isolated from the host's src/. Built into the host's public/ so
// it's served same-origin at /app/remotes/hello-react/remoteEntry.js.
const here = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: here,
  base: "/app/remotes/hello-react/",
  plugins: [
    react(),
    federation({
      name: "hello_react",
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
  build: { target: "esnext", outDir: "../../public/remotes/hello-react", emptyOutDir: true },
});
