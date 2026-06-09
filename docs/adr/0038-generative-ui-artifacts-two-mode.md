# ADR 0038 — Generative-UI artifacts + a two-mode plugin UI (retire federation)

**Status:** Proposed — supersedes the federation parts of [ADR 0034](./0034-plugin-ui-first-class-react.md).

## Context

ADR 0034 made plugin React views first-class via **Module Federation** (in-process remotes sharing
the host's React, behind a trust gate). Shipped in v0.30.0. But two later realisations show
federation was the wrong tool for our actual needs:

1. **Forks** want to add components *without editing core* and *pull upstream cleanly*. A fork
   **rebuilds the app**, so it never needs runtime remote loading — it needs a **build-time seam**.
2. The one case that *seems* to need runtime loading — **AI-generated artifacts** (the agent emits
   mermaid / HTML / a React component on demand; "let AI build UI/plugins on the fly") — is
   **untrusted code generated at runtime**, which is the textbook **sandbox** case, not federation.

**Due diligence — how the field does generative UI** (all sandbox, none federate):
- **Claude Artifacts** — React/HTML in a **sandboxed iframe**; code passed via `window.postMessage`;
  transpiled+bundled for the frame; **CSP locked to one CDN**; curated lib allowlist.
- **Open WebUI Artifacts** — `sandbox="allow-scripts"` iframe, no same-origin by default; docs say
  *"the same isolation model as CodePen, JSFiddle, and Claude Artifacts."*
- ChatGPT Canvas: same iframe-sandbox model. (Codex is code *execution* in a container — different.)

Federation is heavier than a fork needs and less safe than an artifact needs. It occupies an empty
middle. This ADR collapses to two modes and retires it.

## Decision

### D1 — Two modes, by trust

| Need | Mechanism | Trust |
|---|---|---|
| Third-party drop-in plugins | **iframe sandbox** (ADR 0026) | untrusted |
| **AI-generated artifacts** (mermaid/HTML/SVG/React on demand) | **iframe sandbox** + transpile + curated imports | untrusted |
| Fork / first-party in-app components | **build-time `src/ext/` registry** | trusted (rebuilds) |

### D2 — Retire Module Federation

Remove `@originjs/vite-plugin-federation`, `FederatedView`, the `ui: react` *remote* path, the
`@protoagent/plugin-ui` package as a **federation singleton SDK**, and the **react-vs-iframe trust
gate** (`_SHIPPED_TRUSTED_PLUGINS` / `plugins.trusted` / the "Trust React" toggle). The
context-menu registry (which only needed to be a federation singleton to cross the remote boundary)
moves back **host-internal**.

### D3 — The build-time fork seam (`src/ext/`)

Core auto-discovers a fork-owned directory it never edits:

```ts
import.meta.glob("../ext/*.tsx", { eager: true });   // core, once
```

A fork drops `src/ext/<thing>.tsx` that calls `registerSurface(...)` / `registerContextMenu(...)`.
Core ships `src/ext/` **empty** → `git pull upstream` never conflicts; the fork rebuilds. This is
the frontend of the [[operator-fork-contract]] ("forks ADD, never EDIT; core exposes seams").

### D4 — The artifact plugin (first-party, generative UI)

A first-party **`artifact`** plugin on the iframe-sandbox path — the "AI generates UI on demand"
vehicle, built exactly like Claude/Open WebUI:
- A **sandboxed iframe** (`sandbox="allow-scripts"`, `srcdoc`, no same-origin) that the agent's
  generated content renders into; the host `postMessage`s the code in.
- **HTML / SVG / mermaid** render directly; **React/JSX** transpiles in-frame (Babel standalone)
  against a **curated import map / CSP** (React, the design tokens, a small allowlist).
- The agent writes artifacts via a tool (e.g. `show_artifact(kind, code)`); they render in the
  artifact surface. No host access (sandboxed) — generated code can't touch the console.

### D5 — Notes moves off federation

Notes was the only federation consumer. Its **backend stays a plugin** (tools + route + storage);
its **UI becomes a build-time first-party component** via the D3 seam (in-process React, no
federation, no iframe) — making Notes the reference for the fork seam.

## Consequences

- **Less infrastructure, two clean modes** — sandbox for everything untrusted (third-party +
  generative), build-time registry for everything trusted (fork/first-party). Matches the field.
- **Un-ships the federation parts of v0.30.0** — days-old, only Notes used it; acceptable cost for
  the right shape. (ADR 0034's *goal* — rich plugin UI — stands; only its *mechanism* changes.)
- **A genuinely exciting first-party feature** — generative-UI artifacts, the way Claude does it.
- **Security stays sound** — generated/third-party code is sandboxed (iframe, no host access);
  trusted code is the fork's own build. No in-process untrusted code anywhere.

## Build order (proposed slices)

1. **`src/ext/` fork seam + migrate Notes UI** to a build-time component (Notes off federation).
2. **Retire federation** — remove the plugin/vite/SDK/trust-gate machinery now unused.
3. **Artifact plugin** — the sandboxed generative-UI renderer + the agent tool.

## References

- ADR 0034 (federation — superseded here), ADR 0026 (iframe plugin surfaces — the sandbox path this
  builds on), ADR 0027 (git-URL install). Claude Artifacts (reverse-engineered) + Open WebUI
  Artifacts docs (the sandbox-iframe + postMessage + curated-import model this copies).
