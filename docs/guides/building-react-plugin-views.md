# Building a React plugin view

A plugin can ship a **first-class React view** (ADR 0034) that mounts directly into the console's
React tree — sharing the host's React, query cache, and auth — instead of a sandboxed iframe. The
bundled **`notes` plugin** (`plugins/notes/`) is the reference implementation. This guide walks the
four moving parts.

## 1. Declare a `ui: react` view in the manifest

```yaml
# protoagent.plugin.yaml
views:
  - {
      id: notes, label: "Notes", icon: "FileText", placement: right,
      ui: react, path: "/api/plugins/<id>/view",   # iframe fallback for untrusted hosts
      remote: { url: "/app/remotes/<name>/assets/remoteEntry.js", module: "./Panel" },
    }
```

`remote.url` is the built remoteEntry; `module` is the exposed component. `path` is the iframe the
host falls back to if the plugin isn't trusted (see step 4).

## 2. Build the remote (Module Federation)

A small Vite build that exposes your component and **shares** the host singletons — never bundle
your own React/query or you'll dual-load (broken hooks). Mirror `apps/web/remotes/notes/vite.config.ts`:

```ts
federation({
  name: "notes_panel",
  filename: "remoteEntry.js",
  exposes: { "./Panel": `${here}/Panel.tsx` },
  shared: {
    react: { requiredVersion: false },
    "react-dom": { requiredVersion: false },
    "@tanstack/react-query": { requiredVersion: false },
    "@protoagent/plugin-ui": { requiredVersion: false }, // the host bridge + context-menu registry
  },
})
```

Add it to `apps/web` `build:remotes` so it builds into `public/remotes/<name>/`.

## 3. Talk to the host via `@protoagent/plugin-ui`

The SDK is the only thing your component imports from the host. The **host bridge** gives you the
authed API client + context without importing host internals:

```tsx
import { getHostBridge, registerContextMenu } from "@protoagent/plugin-ui";

const { apiUrl, authToken, brandName } = getHostBridge();
// fetch a plugin route with the operator bearer:
fetch(apiUrl("/api/plugins/notes/note"), { headers: { Authorization: `Bearer ${authToken()}` } });

// contribute to the console's right-click menus (merged into the host's, deduped):
registerContextMenu({ type: "rail-surface", items: [{ id: "x", label: "…", run: () => {} }] });
```

Your backend (`register_tools` for the agent, `register_router(prefix="/api/plugins/<id>")` for the
UI's data — under `/api/` so it inherits the operator bearer gate) owns the data. See
`plugins/notes/__init__.py`.

## 4. Trust (ADR 0034 D5)

A `ui: react` view mounts **in-process** with full access to the host — so it only does so if the
plugin is **host-trusted**: in the shipped allowlist (`graph/plugins/loader.py`
`_SHIPPED_TRUSTED_PLUGINS`, first-party) **or** added to `plugins.trusted` by the operator (the
"Trust React" toggle in the Plugins surface). Untrusted? The view degrades to the sandboxed iframe
of `path`. Trust is **host-decided, never declared by the plugin manifest.**
