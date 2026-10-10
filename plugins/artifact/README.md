# artifact-plugin

A **protoAgent plugin** that gives the agent generative UI on demand. The agent calls
`show_artifact(kind, code)` to render **HTML / Markdown / SVG / Mermaid / React** into the console's
Artifact panel — rendered in a **sandboxed iframe** (`sandbox="allow-scripts"`, no same-origin), the
same isolation model as Claude Artifacts / Open WebUI. Generated code runs, but can't touch the
console. React artifacts can `import` a curated **offline** set — charts, icons, and the protoLabs
**design-system** components.

It ships Python tools, a self-served iframe page, and a bundled skill. Plugin
authors can use it as an example of a view that needs no host frontend build.

## Install

Artifact is bundled and enabled by default. To re-enable it, use **Settings →
Plugins → Installed**, or declare it in config:

```yaml
plugins:
  enabled: [artifact]
```

Save the enabled state to mount its console view live. For everyday file creation
and downloads, see [Work with documents and files](../../docs/guides/documents-and-files.md).

## What it adds

- **Tools** — an artifact is a **version chain** (the Claude "update vs rewrite" model), so editing
  iterates the same artifact instead of flooding the panel with near-duplicates:
  - `show_artifact(kind, code, title, links?)` — **create** (`kind` ∈ `html` · `markdown` · `svg` ·
    `mermaid` · `vega-lite` · `react`). `markdown` renders with design-system prose styling
    (` ```mermaid ` fences become live diagrams); `vega-lite` is a **chart** — see **Charts** below;
    `react` can `import` the curated libraries below. `links` (mermaid) — see **Code-linked
    diagrams** below.
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
toolbar (− · zoom % · + · Fit · Reset). Small diagrams open fitted (never above 1:1). A diagram
whose labels would shrink below ~11px when fitted (a long sequence diagram in a narrow dock) opens
at fit-to-width — or the smallest readable zoom — anchored at the top-left instead; **Fit** always
shows the whole diagram and **Reset** returns to that start view. Mermaid's palette follows the
console theme (dark/light).
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
- **Anchors**: each target should carry `anchor` — a short exact snippet of the line it means
  (`"runTool("`, ≤ 120 chars, one line). The server finds it in the file (exact, else ignoring
  whitespace) and SNAPS the link to the occurrence nearest the given `line`, keeping the range's
  length; the reply reports each move ("moved 59→66 to match anchor 'runTool('"). An anchor
  that isn't in the file drops the link. Models miscount lines but quote code reliably, so this
  makes landing deterministic. The anchor is stored with the link; links without one work as
  before, and the reply nudges to add it.
- **Validation** mirrors `show_code`: the project fence (`live_project_registry`), the secret-path
  deny list, the file exists and is text, the line is in range (`end_line` clamped), the note ≤ 280
  chars. A bad link is **dropped with a reason** — the artifact still lands. The reply echoes each
  target's first line (so the model can check it pointed where it meant) and names keys that
  match nothing in the diagram. Needs the filesystem toolset.
- **Storage**: links live on the **version** (`versions[i].links`), so an older version and its chat
  chip keep their own. `update_artifact` and a panel edit carry them forward; a rewrite doesn't.
- **In the panel**: linked elements get a dotted underline and a hover/focus tooltip
  (`project/path:line — note`); they're keyboard-focusable (Enter opens). A **Links (n)** button
  (a single-line badge) lists every link (arrow keys, Enter, Esc), highlights the element on hover/focus and pans to it,
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

## Charts (`vega-lite`, ADR 0116)

A chart is a [Vega-Lite](https://vega.github.io/vega-lite/) spec with its rows **inline** in
`data.values` — a few hundred bytes of JSON instead of a hand-written component, which is what
makes a chart fast to produce. The panel draws it with the vendored `vega` 6.4.0 / `vega-lite`
6.4.3 / `vega-embed` 7.3.0 (BSD-3-Clause; their own UMD builds, byte-for-byte, notices in
`vendor/vega.LICENSES.txt`), SRI-pinned like the other UMD libs.

- **Themed for you.** The chart takes the console's own data-viz tokens — `--pl-color-chart-series1…8`
  as the categorical palette, `--pl-color-chart-axis`/`-grid` for guides, the theme's fg/bg/font —
  and redraws on a live theme switch. Leave colours and background out of the spec; a spec's own
  `config` still wins when you mean one. A single view fills the panel width.
- **Inline data only.** A `url` inside any data definition is refused when the version is written
  (create, update and rewrite), with a reason. In the frame, Vega's loader refuses every load
  (`data.url`, a spec by URL, image marks), the CSP has no network (`connect-src 'none'`, images and
  fonts `data:` only), expressions run as the CSP-safe interpreter (no `eval`, so no
  `'unsafe-eval'`), and a spec's `usermeta.embedOptions` — which vega-embed would otherwise let
  override the embed options, loader included — is stripped. No export/editor action menu.
- **From another plugin.** The `artifact.show` plugin service creates one without importing this
  plugin — `graph.sdk.service("artifact.show")(kind="vega-lite", code=spec_json, title=…)` → `{ok,
  id, version, message, ref}`; append `ref` (the chat chip) last. The data plugin's `data_chart`
  runs a query and hands its rows here this way.

```json
{"title": "Revenue by weekday", "mark": {"type": "bar", "tooltip": true},
 "encoding": {"x": {"field": "weekday", "type": "nominal", "sort": "-y"},
              "y": {"field": "revenue", "type": "quantitative", "axis": {"format": "$,.0f"}}},
 "data": {"values": [{"weekday": "Sat", "revenue": 2310.5}, {"weekday": "Fri", "revenue": 1876}]}}
```

## File artifacts — what each type previews as

`save_file_artifact(path, title?, artifact_id?, project?)` stores a generated file's bytes as a
versioned artifact (Download button, version history) and previews it by type. `path` takes the
fs tools' form: a project-relative path (pass `project`, or omit it when one project has the file)
or an absolute path. Every version also keeps a **text projection** in `code`, so history stays
diffable and screen readers / a failed render still have the content.

| File | Preview | Safety gate |
|---|---|---|
| `.docx` | **Real pages** — Word styles, headings, lists, tables, images, headers/footers, footnotes; − / Fit / + zoom ([below](#word-documents-docx)) | save-time zip preflight (`_docx.py`) |
| `.pdf` | **Real pages** — continuous scroll, zoom ([below](#pdfs-pdf)) | save-time stream preflight (`_pdfview.py`) |
| `.pptx` | **Real slides** — current slide + filmstrip ([below](#slide-decks-pptx)) | save-time zip preflight (`_slides.py`) |
| `.csv` / `.tsv` | **Table** — delimiter sniffed (`,` `;` tab `|`), Excel's byte-order mark stripped, numeric columns right-aligned, first 500 rows | — (text) |
| `.xlsx` | **One table per sheet** (first 200 rows × 50 columns each) | — (text projection) |
| `.md` | Rendered prose | — |
| `.json` | Pretty-printed | — |
| images | Thumbnail | — |
| other text | Plain text | — |

The text projection is capped by `max_preview_kb`; a clipped preview says so and points at Download.

## Slide decks (.pptx)

A `.pptx` saved with `save_file_artifact` previews as its **real slides**, not a text dump: a large
current slide sized to the panel with a filmstrip under it. Arrow keys / PageUp / PageDown / Home /
End page through it, clicking the slide advances (its left third goes back), and the filmstrip
thumbnails jump. Slides are laid out as HTML/SVG, so text stays sharp at any width; the
filmstrip only lays out the thumbnails near the visible strip, so a 300-slide deck opens as fast
as a 6-slide one. The text outline stays under the slides (collapsed) for screen readers and
copy-paste, and is the whole view when a deck can't be rendered.

The renderer is [`@aiden0z/pptx-renderer`](https://github.com/aiden0z/pptx-renderer) 1.3.0
(Apache-2.0; bundles JSZip, ECharts/ZRender and the MPL-2.0 `mtx-decompressor`, notices in
`vendor/pptx-renderer.LICENSES.txt`), vendored as `vendor/pptx-renderer.min.js` and SRI-pinned like
the other UMD libs. It supports theme colours and fonts, placeholders and master/layout
inheritance, bullets, tables, images, preset shapes, gradients, charts and SmartArt fallbacks.
Fonts a deck names but the machine lacks fall back to a system font, and EMF/WMF vector art without
an embedded preview is not drawn.

**Hostile files.** The deck renders inside the same no-same-origin sandbox as every artifact,
under a nonce Content-Security-Policy (no inline handlers, no `javascript:` URLs, `connect-src
'none'`, images/fonts/media from `blob:`/`data:` only), and only plain `http(s)` links keep an
`href` (the sandbox can't open them anyway). The frame can't authenticate, so the shell fetches the
gated blob and transfers the bytes in — and only for a version the save-time preflight
**cleared**. That preflight (`_slides.py`) doesn't trust the zip's declared sizes: it inflates
every entry's raw stream in bounded chunks against a running byte budget and a 15 s clock, then
requires the real size and CRC to match the declared ones (a header that lies is a tampered
zip). Its verdict is stamped on the version as `file.slides`, so a view never re-inflates
anything; a refused deck, or one saved before slide previews existed, shows the outline. Caps
(`PPTX_CAPS` in `shell.js` mirrors them, drift-guarded): 40 MB file, 4000 entries, 32 MB per
inflated entry, 256 MB inflated in total, a 200:1 compression ratio on big entries, 1000 slides,
50 megapixels per image and 150 MP across the deck (images past either become placeholders, never
decoded). The frame re-applies them on its own inflate, one entry at a time so the first cap hit
stops it, with a 20 s parse budget; a 45 s shell watchdog swaps in the outline card if the frame
never answers.

## PDFs (.pdf)

A `.pdf` saved with `save_file_artifact` previews as its **real pages**: one continuous scroll,
fitted to the panel width, with a page indicator (‹ 3 / 12 ›) and zoom (− / Fit / +, or the `-`,
`0` and `+` keys). Pages are drawn to canvases only while they're near the visible area and freed
when they scroll away, so a 500-page report opens as fast as a 3-page sheet; a drawn page's canvas
is capped at 16 MP whatever the zoom. The extracted text stays underneath (collapsed) for screen
readers, copying, and as the whole view when a file can't be rendered.

The renderer is [pdf.js](https://github.com/mozilla/pdf.js) (`pdfjs-dist` 6.4.299, legacy build,
Apache-2.0 — notices in `vendor/pdfjs.LICENSES.txt`), vendored byte-for-byte as
`vendor/pdfjs.min.mjs` + `vendor/pdfjs-worker.min.mjs` and SRI-pinned. It runs on the frame's
**main thread**: the worker module only registers pdf.js's message handler on `globalThis`, so the
frame never starts a Worker (its CSP forbids them). No wasm decoders, cMaps or standard-font data
are vendored: JPEG 2000 / JBIG2 images draw blank and non-embedded fonts fall back to system fonts.
There's no text or link layer (pages are pictures; use the extracted text to copy).

**Hostile files.** Same sandbox and nonce CSP as slides, plus `worker-src 'none'` and no network of
any kind (`useWasm:false`, no streaming or range loads). pdf.js has no decompression limits of its
own, so the save-time preflight (`_pdfview.py`) decodes every stream the renderer will decode —
each page's content streams, its image and form XObjects (forms recursively) and embedded font
programs — through pypdf's bounded filters, against a 64 MB per-stream and 512 MB whole-document
budget and a 15 s clock. Image sizes come from the image dictionaries (no decode): 50 MP per
image, 150 MP drawn on one page. Password-protected files, unreadable files and anything over
40 MB or 2000 pages are refused. The verdict is stamped on the version as `file.pdf`; a refused
PDF, or one saved before page previews existed, shows the extracted-text card with the reason.
`PDF_CAPS` in `shell.js` mirrors the caps (drift-guarded); the frame adds a 20 s parse budget and
the shell's 45 s watchdog swaps in the text card if the frame never answers.

## Word documents (.docx)

A `.docx` saved with `save_file_artifact` previews as its **real pages**: the document's own page
size and margins, Word paragraph/character styles, headings, numbered and bulleted lists, tables,
embedded images, headers, footers, footnotes and endnotes, fitted to the panel width with a page
indicator and zoom (− / Fit / +, or the `-` `0` `+` keys). Scaling uses CSS `zoom`, so text is
re-laid-out and stays sharp (a scaling transform is blurred by WKWebView, #1517). The extracted text
stays underneath (collapsed). Word's Symbol/Wingdings bullet code points are swapped for real
Unicode bullets after rendering, since browsers don't ship those fonts.

The renderer is [docx-preview](https://github.com/VolodymyrBaydalka/docxjs) 0.4.1 (Apache-2.0) on
[JSZip](https://github.com/Stuk/jszip) 3.10.2 (MIT), both vendored byte-for-byte as UMD builds
(`vendor/jszip.min.js`, `vendor/docx-preview.min.js`) and SRI-pinned; notices in
`vendor/docx-preview.LICENSES.txt`. Not rendered: tracked changes and comments (off), and
**altChunks** (raw HTML embedded in a document — never rendered). Legacy binary `.doc` files keep
the text card.

**Hostile files.** Same sandbox and nonce CSP as slides/pages (no network, no workers). A .docx is a
zip, so the save-time preflight (`_docx.py`) reuses the slide preflight's bounded inflater: every
entry is actually inflated against per-entry (32 MB) and whole-archive (256 MB) budgets and a 15 s
clock, and its real size + CRC must match the directory. Images are measured from their headers:
over 50 MP for one, or 400 MP for the whole document, refuses the preview with the reason. Anything
over 40 MB, unreadable, or without `word/document.xml` keeps the text card. The verdict is stamped
on the version as `file.docx`. `DOCX_CAPS` in `shell.js` mirrors the caps (drift-guarded); the frame
adds a 20 s parse budget and the shell's 45 s watchdog swaps in the text card if it never answers.
Only plain `http(s)` and in-document `#` links keep an `href`.

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

State (`history.json` plus the sidecar `blobs/`) lives in the instance's plugin store —
`<instance_root>/artifact` (ADR 0004 / ADR 0065), so a box-scoped server (`PROTOAGENT_BOX_ROOT`)
and every fleet member get their own copy. `ARTIFACT_DIR` is an env-only override of that directory
(still `/<PROTOAGENT_INSTANCE>`-scoped when set). Legacy data from before instance scoping is
migrated into the new location automatically on first access — the `history.json` and its `blobs/`
move together (moving only the JSON would break file-artifact downloads). The migration source is
the store's OLD location, `~/.protoagent/artifact[/<PROTOAGENT_INSTANCE>]` — HOME-relative, exactly
where the pre-scoping code wrote it, **independent of `PROTOAGENT_BOX_ROOT` / `PROTOAGENT_HOME`**.
That matters because the desktop and containers point the box root at their own directory (Tauri's
config dir; `/sandbox`) while the old store was still written under HOME: reading the legacy source
from HOME is what keeps a desktop or container upgrade from silently losing its history and pins.
Sibling instance subdirectories under a bare legacy dir are left where they are, and if the new
location already has a `history.json` it wins outright — no migration.

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
> - **UMD `<script>` libs** — React, ReactDOM, Babel, Mermaid, the `.pptx` slide renderer, the `.docx` renderer (JSZip + docx-preview), and Vega / Vega-Lite / vega-embed (`*.min.js`). The `.pdf` renderer (pdf.js) is the same idea as ES modules (`pdfjs*.min.mjs`, `<script type="module">`, also SRI-pinned). Pinned with **Subresource
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
