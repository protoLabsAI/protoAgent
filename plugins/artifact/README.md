# artifact-plugin

A **protoAgent plugin** that gives the agent generative UI on demand. The agent calls
`show_artifact(kind, code)` to render **HTML / Markdown / SVG / Mermaid / React** into the console's
Artifact panel — rendered in a **sandboxed iframe** (`sandbox="allow-scripts"`, no same-origin), the
same isolation model as Claude Artifacts / Open WebUI. Generated code runs, but can't touch the
console. React artifacts can `import` a curated **offline** set — charts, icons, and the protoLabs
**design-system** components.

It's also the **reference external plugin**: pure Python + a self-served iframe page + a bundled
skill — no host build, no federation. Installable from this git URL.

## Install

In the protoAgent console: **Plugins → Download → install from a git URL**, or in config:

```yaml
plugins:
  enabled: [artifact]
```

then install `https://github.com/protoLabsAI/artifact-plugin` (ADR 0027). Restart to mount its
console view.

## What it adds

- **Tools** — an artifact is a **version chain** (the Claude "update vs rewrite" model), so editing
  iterates the same artifact instead of flooding the panel with near-duplicates:
  - `show_artifact(kind, code, title, links?)` — **create** (`kind` ∈ `html` · `markdown` · `svg` ·
    `mermaid` · `react`). `markdown` renders with design-system prose styling (` ```mermaid ` fences
    become live diagrams); `react` can `import` the curated libraries below. `links` (mermaid) —
    see **Code-linked diagrams** below.
  - `update_artifact(old_string, new_string, artifact_id?, links?)` — **targeted edit**
    (string-replace, must match once) → new version. The fast path for small changes. A
    diagram's links carry over unless `links` is passed (`{}` clears them).
  - `rewrite_artifact(code, title?, artifact_id?, links?)` — **full replace** → new version (links
    don't carry over a rewrite — pass them).
  - `get_artifact(artifact_id?)` — **read the current source** (kind/title/version + code), so you can
    take over an artifact you didn't author (read it, then `update_artifact`/`rewrite_artifact`).
  - `check_artifact(artifact_id?)` — the latest **render verdict** (rendered cleanly / failed with the
    captured error / no result yet), so a render failure feeds back into a fix instead of a silent blank.
  - `pin_artifact(artifact_id, pinned?)` — **pin** a long-lived artifact (a master resume, a
    reference doc) so history eviction skips it; `pinned=False` unpins. Pins are capped by
    **Pinned artifacts** below (refused at the cap), don't count toward **Artifacts kept**, and
    still trim to **Versions per artifact** — a pin keeps the artifact, not every edit. Pinned
    artifacts are listed first. Downgrading the plugin below 0.18.0 drops pin protection: the
    older plugin evicts by recency again, so pinned artifacts can be evicted.
  - `list_artifacts()` / `delete_artifact(artifact_id)` — manage them (`list_artifacts` and
    `get_artifact` mark pinned ones).
- **View** "Artifact" (right rail) — a sandboxed renderer with an **artifact picker**, **version
  navigation** (step back/forward through edits), an **in-panel code editor** (edit the source and
  *Run & save* → a new `user` version, never overwriting the agent's), **download** (this version),
  and **delete**.
- **Chat chip** `artifact-ref` (#3617) — every create/revise (`show_artifact`, `update_artifact`,
  `rewrite_artifact`, `save_file_artifact`) leaves a chip in the transcript — `✨ <title> · v<n>`
  plus the kind — that opens the panel on **exactly that artifact and version**, the way the
  code pane's `code-ref` chip does. The model still sees the same text ("Updated artifact X →
  version N"); the chip rides behind it as a component-v1 payload the host lifts out (the plugin
  registers the kind + its validator with `registry.register_component`). Behaviour:
  - the **live** turn opens the panel on the new version (desktop only, only for the chat on
    screen, and never onto chat's own dock); history hydration and reattach never do, so a reload
    doesn't reopen it. On a phone nothing opens by itself — a tap pushes the panel (ADR 0086);
  - a chip for an **older version** opens that version, reads `v2 of 5`, and turns the panel's
    follow-newest off so the next agent edit doesn't yank the operator away from it;
  - a **deleted/evicted** artifact (or a version trimmed at the *Versions per artifact* cap)
    renders the chip inert — "no longer available" — and a chip whose panel is off (plugin
    disabled) renders inert too;
  - the chip carries a POINTER only (id, lifetime version number, title, kind) — never the code —
    so nothing more persists in chat history than the tool's own text already did (incognito
    included).

  `version` is the artifact's **lifetime** version number: past the cap the oldest versions are
  trimmed, and the panel labels versions the same way (`v48 of 52`), so a chip keeps naming the
  version it was made for.
- **Events** `artifact.created` / `artifact.updated` / `artifact.deleted` (ADR 0039) — broadcast on
  the bus so the console lights the Artifact rail icon even when the panel is closed.
- **Skills** `rendering-artifacts` — teaches render-don't-write-files and the edit-don't-recreate
  workflow; `diagramming-code` — grounded, code-linked mermaid diagrams (read first, cite real
  lines, pick the right diagram, keep it small, revise the same artifact).

## Navigable diagrams

svg and mermaid artifacts (and ` ```mermaid ` fences inside markdown) render into a navigable
viewport: **wheel / pinch** zoom to the cursor (Safari/WKWebView gesture events included), **drag**
to pan, **double-click** to zoom in (shift: out), keys **+ − 0 f** and the **arrows**, and a
toolbar (− · zoom % · + · Fit · Reset). Big diagrams open fitted to the frame; small ones at 1:1.
Every zoom moves the root `<svg>`'s **viewBox** — the vector is re-laid out, so it stays sharp at
any zoom. (A CSS-transform zoom was removed in #1517 because WKWebView blurred it.) In markdown
the plain wheel keeps scrolling the page; zoom there with ctrl/⌘ + wheel, a pinch, or the toolbar.
Animated steps respect `prefers-reduced-motion`.

## Code-linked diagrams

A mermaid version can carry `links`, so the operator clicks a node or a message and lands on the
code — in the console's code pane (ADR 0112, `filesystem.code_pane`), else their external editor
(Settings ▸ Chat ▸ Open files in), else the path is copied:

```text
show_artifact(kind="mermaid", code="sequenceDiagram\n  C->>A: ask(q)\n  A->>T: run_tool(call)",
  links={"msg:1": {"project": "app", "path": "src/agent.py", "line": 40, "end_line": 58,
                   "note": "ask() builds the prompt and starts the tool loop"},
         "participant:A": {"project": "app", "path": "src/agent.py", "line": 12}})
```

- **Keys**: a flowchart / class / state node id (or a flowchart subgraph id) as written;
  `participant:<id or alias>`; `msg:<n>` (the n-th sequence message, 1-based, source order) or
  `msg:<exact label>` when that label is unique.
- **Validation** mirrors `show_code`: the project fence (`live_project_registry`), the secret-path
  deny list, the file exists and is text, the line is in range (`end_line` clamped), the note ≤ 280
  chars. A bad link is **dropped with a reason** — the artifact still lands. The reply echoes each
  target's first line (so the model can check it pointed where it meant) and names keys that
  match nothing in the diagram. Needs the filesystem toolset.
- **Storage**: links live on the **version** (`versions[i].links`), so an older version and its chat
  chip keep their own. `update_artifact` and a panel edit carry them forward; a rewrite doesn't.
- **In the panel**: linked elements get a dotted underline and a hover/focus tooltip
  (`project/path:line — note`); they're keyboard-focusable (Enter opens). A **Links (n)** button
  lists every link (arrow keys, Enter, Esc), highlights the element on hover/focus and pans to it,
  and marks links that match nothing in the diagram.
- **Trust**: the sandboxed frame posts only a KEY (`protoArtifact:openCode`), and only behind a
  user gesture; the shell resolves it against the rendered version's stored links and forwards
  that target to the console (`protoagent:code:open`), which accepts it only from the plugin's own
  iframe at its origin. A path the frame names is never used; notes and labels render as text.

## Curated React imports + the design system

`react` artifacts can `import` from a curated, **fully-offline** set (resolved by an
[import map](https://developer.mozilla.org/en-US/docs/Web/HTML/Element/script/type/importmap) to the
same-origin `vendor/` modules — no network):

| Specifier | What |
|---|---|
| `@pl/ui` | protoLabs **design-system** wrappers that match the console theme — enough to prototype real **layouts** + **components**, not just widgets. Layout: `AppShell` · `Header` · `SideNav`/`SideNavItem` · `Container` · `Section` · `Panel` · `Grid` · `Row` · `Hero` · `Divider`. Nav: `Tabs`/`Tab` · `Segmented`/`SegmentedButton` · `Menu`/`MenuItem`/`MenuSeparator`. Data: `Table` · `Board` · `Stats`/`Stat` · `Steps`/`Step` · `Progress` · `Accordion`/`AccordionItem` · `Avatar` · `Empty`. Overlays: `Dialog` · `Drawer` · `Callout` · `Tip`. Forms: `Field` · `Input` · `Textarea` · `Select` · `Switch` · `Checkbox`. Primitives/type: `Button` · `IconButton` · `Card` · `Badge` · `Tag` · `Kbd` · `Link` · `Dot` · `Spinner` · `Skeleton` · `Alert` · `Heading` · `Eyebrow` · `Lead` · `Prose` · `Icon` (lucide by `name`). |
| `chart.js` | `import { Chart } from 'chart.js'` (controllers pre-registered) — quick charts onto a `<canvas>`. |
| `d3` | `import * as d3 from 'd3'` — bespoke data-driven SVG. |
| `lucide` | the raw icon library (if not using `@pl/ui`'s `Icon`). |
| `react`, `react-dom/client` | resolve to the same React the UMD globals use (one shared instance). |

The design system ships only `.tsx` source (no browser ESM build), so `@pl/ui` is a set of
**authored** wrappers over the DS `.pl-*` classes — mirroring the class contracts of the ~90
components the injected `plugin-kit.css` already styles, so a React artifact can compose real
layouts + components rather than hand-rolling class strings. Those classes and the `--pl-*` tokens
are injected into every `html` / `react` / `markdown` artifact (via the host-served
`/_ds/plugin-kit.css`), so even plain elements (`className="pl-btn pl-btn--primary"`) follow the
live theme — reach for a raw element with `className="pl-…"` for any component `@pl/ui` doesn't wrap.

## Configuration

The operator-facing knobs are **Settings ▸ Plugins ▸ Artifact** fields (no restart) — and an
environment variable of the same knob overrides the UI for headless / ACP setups. Precedence:
**env > Settings ▸ Plugins > default**.

| Setting (Settings ▸ Plugins) | Env override | Default | What |
|---|---|---|---|
| **Interactive artifacts** | `ARTIFACT_ASK_ENABLED` | _off_ | Let artifacts call back to the agent via `window.protoArtifact.ask()` (below). |
| **Ask system instruction** | `ARTIFACT_ASK_SYSTEM` | _(none)_ | Optional system prompt wrapping every `ask()`. |
| **Ask prompt limit (chars)** | `ARTIFACT_ASK_MAX_CHARS` | `4000` | Max prompt length for an `ask()`. |
| **Artifacts kept** | `ARTIFACT_HISTORY` | `20` | How many unpinned artifacts to keep (oldest evicted; pinned ones don't count). |
| **Versions per artifact** | `ARTIFACT_MAX_VERSIONS` | `50` | Max versions kept per artifact, pinned or not (oldest edits trimmed). |
| **Pinned artifacts** | `ARTIFACT_MAX_PINNED` | `10` | Max pinned artifacts; a pin past this is refused. `0` refuses all new pins. Lowering it (to `0` included) doesn't unpin anything: existing pins stay protected until unpinned. |
| **Max artifact size (KB)** | `ARTIFACT_MAX_CODE_KB` | `512` | Max source size per version (a larger render is rejected). |

`ARTIFACT_DIR` (`~/.protoagent/artifact`) is env-only — where state is stored (instance-scoped by
`PROTOAGENT_INSTANCE`).

## Interactive artifacts (calling back to the agent)

Every artifact gets a **`window.protoArtifact.ask(prompt)`** helper — the
[`window.claude.complete`](https://claude.com/blog/claude-powered-artifacts) analog. It returns a
Promise that resolves to the agent's answer, so an artifact can be a mini-app — an AI game NPC, a
tutor, a content generator:

```js
const reply = await window.protoArtifact.ask("Give the NPC a gruff one-line greeting.");
```

It's **opt-in** — flip **Interactive artifacts** on in **Settings ▸ Plugins ▸ Artifact** (or set
`ARTIFACT_ASK_ENABLED=1`); letting sandboxed artifact code trigger LLM calls is a cost surface.
Under the hood the sandboxed artifact `postMessage`s the shell, which calls the
**bearer-gated** `POST /api/plugins/artifact/ask` → a *bare* completion via the host SDK
(`graph.sdk.complete`, protoAgent ≥ the build that ships it). When disabled or unsupported, `ask()`
rejects with a clear message. The artifact sandbox stays opaque-origin throughout — the bridge is
the only channel out.

## Routes

The shell **page** is public at `/plugins/artifact/view` (an iframe page-load can't carry a
bearer, and the page derives its slug base from `/plugins/…`); its **data/action** routes
(`/current`, `/history`, `/refs`, `PUT`/`DELETE` `/artifact/{id}`, `POST /ask`) are gated under
`/api/plugins/artifact`. `GET /refs?ids=a,b` is the chat chips' metadata — for each id still in
the store its title, kind, lifetime `version_count` and `oldest` kept version, never code. Page chrome is the protoLabs design-system kit
(`/_ds/plugin-kit.{css,js}`), so the panel follows the operator's live theme.

## Security

Generated artifacts are untrusted (prompt injection) and run **sandboxed** — a nested
`<iframe sandbox="allow-scripts">` with **no** `allow-same-origin`, so the code runs but can't
reach the console, its cookies, or its APIs (the Claude Artifacts / Open WebUI model). See
protoAgent's
[security & trust model](https://github.com/protoLabsAI/protoAgent/blob/main/docs/explanation/security-and-trust.md).

The chat chip's deep-link is an inbound `protoArtifact:select {id, ver}` message. The shell honours
it only from the window that **embeds** it (`e.source === window.parent`, and `e.origin` must equal
`location.ancestorOrigins[0]` where the browser provides it) — never from the nested artifact
frame, so model-authored code can't drive the panel's selection. The console posts it only after
the page announces it is listening (`protoagent:ready`), targeted at the page's own origin.

> **Offline / no network.** Everything is **vendored** under `vendor/` and served same-origin from
> `/plugins/artifact/vendor/…`, so every artifact kind renders **fully offline** — no `cdnjs`, no
> outbound network at all (`capabilities.network: []` is literally true):
> - **UMD `<script>` libs** — React, ReactDOM, Babel, Mermaid (`*.min.js`). Pinned with **Subresource
>   Integrity** (`integrity` + `crossorigin="anonymous"` — required because the sandbox is an opaque
>   origin, so the load is cross-origin); a tampered served file won't execute. To bump one, replace
>   the file, recompute its `sha512`, and update the `LIB` map in the shell page.
> - **ESM modules** (the `react` import map) — `d3.mjs`, `chartjs.mjs`, `lucide.mjs`, `marked.mjs`
>   (esbuild-bundled, self-contained) plus the authored `pl-ui.mjs` + `react*.shim.mjs`. These are
>   same-origin and **install-pinned** (the `plugins.lock` commit sha pins the exact bytes) rather
>   than SRI-pinned — import-map `integrity` isn't yet broadly supported. To bump a curated lib,
>   re-bundle it into `vendor/` (`esbuild --bundle --format=esm --minify`).

## Development

```bash
pip install -r requirements-dev.txt
pytest            # the suite
ruff check . && ruff format --check .
```

CI (`.github/workflows/ci.yml`) runs the same on every PR.

---
Built for [protoAgent](https://github.com/protoLabsAI/protoAgent).
