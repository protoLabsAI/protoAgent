# 0118 — Interactive answers: inline artifacts in chat, streamed previews, a send-to-chat bridge, and frame-rendered plugin components

- Status: Proposed
- Date: 2026-10-09
- Builds on: [ADR 0038](./0038-generative-ui-artifacts-two-mode.md) (sandboxed artifacts, iframe vs `src/ext` UI), [ADR 0051](./0051-a2a-realtime-streaming-and-component-rendering.md) (`component-v1`, `show_component`, plugin kinds), [ADR 0061](./0061-frontend-extension-registries.md) (console extension registries), [ADR 0116](./0116-local-first-data-analyst-duckdb-and-vega-lite-charts.md) (the `vega-lite` kind, nonce CSP, vendoring, `--pl-color-chart-*` theming, `artifact.show` service), [ADR 0111](./0111-zed-operator-editor-acp-shim.md) D3 (tool-call args reach the wire only as an 800-char preview).
- Amends: ADR 0051 (a fourth presentation tier between data components and the panel; plugin kinds can render without a console rebuild), ADR 0038 (one artifact, two placements).
- Refs: [CopilotKit/OpenIntelligentUI](https://github.com/CopilotKit/OpenIntelligentUI) (MIT) at `f6e4388`, read 2026-10-09: its `generateSandboxedUi` renderer, Websandbox host bridge and visualization router.

## Context

protoAgent gives the model three ways to show something today:

1. **Markdown text**, including mermaid fences, KaTeX and GFM tables (`apps/web/src/chat/Markdown.tsx`).
2. **Data components.** `show_component` emits a `component-v1` DataPart. The core kinds are `table`, `keyvalue`, `timeline` and `code-ref` (`graph/components.py:28`). They are data only and validated server-side, and the console renders them from a registry (`apps/web/src/chat/ChatComponent.tsx`). Running no code is what makes them safe.
3. **Artifacts.** `show_artifact(kind, code)` puts html / markdown / svg / mermaid / react / vega-lite in the Artifact panel's sandboxed frame. The chat gets only an `artifact-ref` chip.

OpenIntelligentUI ("OIU") is CopilotKit's reference app for answers you can interact with: a calculator, a 3D explainer or a map, rendered **inside the conversation** as the model writes it. Reading its renderer against our stack shows five gaps.

**G1 — A generated tool lives off to the side.** An interactive answer is a chip that opens a panel. Scrollback holds a pointer, not the answer, and two answers can't sit next to each other in the transcript.

**G2 — Nothing renders until the tool call finishes.** The model can spend 30–60 s writing an artifact, and the operator watches a spinner the whole time. The server announces the tool card from the first `tool_call_chunks` with `input: ""` (`server/turn_stream.py`, `_on_chat_model_stream`). Args are filled only at `on_chat_model_end`, and reach the wire only as an 800-char preview (ADR 0111 D3). OIU streams the tool's arguments in a fixed order: `initialHeight → placeholderMessages → css → html → jsFunctions → jsExpressions`. It holds a placeholder until the CSS is complete, then morphs the partial HTML into a script-free preview frame. Scripts run only once the markup is whole.

**G3 — UI can't talk back to the agent.** HITL forms are the only UI that resumes a turn. The artifact `ask` bridge (`plugins/artifact/_routes.py`, `/ask`) runs a *bare, invisible* completion and is opt-in because of cost. No component can do what OIU's `sendPrompt({text})` does: the user clicks "Ask about this scenario", and that starts a normal, visible turn carrying the selected values.

**G4 — Plugins installed from git can't render inline.** A plugin can register a component *kind* (`registry.register_component`), but its renderer has to be a console `src/ext` module compiled into `apps/web/dist`. An installed plugin can't ship one, so its components show as `[unsupported component]` (ADR 0051 amendment #3617).

**G5 — Nothing tells the model which presentation to use.** It gets six artifact kinds, four component kinds and markdown, but no guidance on which fits a request. OIU makes that choice before each turn with an external classifier and keeps an explicit quality bar for interactive output. That bar covers working controls, validated numeric inputs, provenance labels and reduced motion.

We do **not** want OIU's stack:

- **Its transport.** CopilotKit/AG-UI would replace A2A, and A2A clients degrade cleanly to text today (ADR 0051).
- **Its library loading.** It uses an unpinned esm.sh import map, and its CSP allows `'unsafe-eval'` plus four CDNs, which ADR 0116 D5 forbids.
- **Its state store.** The checkpointer is in process memory.
- **Its router.** The router is a paid external API that fails the whole turn when it is down.

We want its *patterns*, implemented on our existing seams.

## Decision drivers

- **The answer lives in the transcript.** An interactive answer scrolls, replays and reattaches with the conversation, like text does.
- **One sandbox, one fence.** Inline UI must not open a second, weaker iframe security model. The artifact frame's nonce CSP, vendored SRI-pinned libraries and deny-everything loader (ADR 0116 D5) are the fence. No kind's CSP is widened by this ADR.
- **The operator watches it being built.** The time to the first visible pixel should be close to the time the model starts writing markup, not the time it finishes.
- **The user's click, the user's turn.** UI may start an agent turn only from a real user gesture, and the result must be indistinguishable in audit from the user typing. It never runs on load, on a timer or on input changes.
- **A2A stays clean.** Everything new is an optional DataPart or a prop. A client that ignores it still gets the text and the chip.
- **Plugins extend without forking or rebuilding** (the ADR 0038/0061 promise, kept for inline rendering too).

## Options considered

| Option | For | Against |
|---|---|---|
| Adopt CopilotKit + AG-UI wholesale | Mature hooks (`useComponent`, `useRenderTool`, `useHumanInTheLoop`), and OIU as a working reference | Replaces the A2A transport, the console chat runtime and our HITL. Adds a Next.js-shaped runtime route. Months of work for capabilities we can reach on our seams. |
| A2UI-style declarative catalog only | Data-only like `component-v1`, safe, portable | Can't express a calculator, a simulation or a 3D explainer. We already have the declarative tier (`component-v1`) and vega-lite for charts. |
| A new core `sandboxed-ui` component kind, separate from artifacts (OIU's shape) | Inline from day one | A second iframe host, CSP, vendoring path, theme bridge and render verdict, all already in the artifact shell. Two fences drift. |
| **Inline placement of artifacts + streamed args + a bridge + frame-rendered plugin kinds (chosen)** | Reuses the artifact frame end to end (fence, libraries, theme, versioning, render verdict, `open in panel`). Each gap closes on an existing seam. | The inline path depends on the artifact plugin being on (it is first-party). Arg streaming is new wire surface. |

For G2, we considered OIU's six-field ordered tool schema. **Rejected:** it forks `show_artifact`'s contract and the artifact store's single-`code` version model. We keep one `code` string and take the ordering as a convention the skill teaches (D3, D7).

For G5, we considered a per-turn routing middleware like OIU's `JevVisualizationMiddleware`, which classifies the turn, filters out the non-matching render tools and injects that renderer's contract. **Deferred, not rejected** (D7).

## Decision

### D1 — Four presentation tiers; inline and panel are two placements of one artifact

| Tier | Mechanism | Runs code? | Use for |
|---|---|---|---|
| Text | markdown (incl. mermaid fences) | no | facts, prose, code, small tables |
| Data component | `show_component` → `component-v1` | no | exact values, records, steps, code pointers |
| **Inline artifact** (new) | `show_artifact(…, placement="inline")` | yes, in the artifact sandbox | an answer the user interacts with *in the conversation*: calculator, explainer, chart, small tool |
| Panel artifact | `show_artifact(…)` (default) | yes, same sandbox | a document or tool worked on over many turns, decks, PDFs, large apps |

An inline artifact **is** an artifact. It has the same store, versions, render verdict, `update_artifact`/`rewrite_artifact` and "Open in panel". Only where it renders differs.

### D2 — Inline placement: the `artifact-ref` chip hosts the artifact's own frame

- **Tool.** `show_artifact` (and the `artifact.show` service from ADR 0116 D6) gains `placement: "panel" | "inline"`. The default is `"panel"`, so existing callers are unchanged. Inline is allowed for `html`, `svg`, `mermaid`, `react` and `vega-lite`. Other kinds (`markdown`, decks, PDFs, `file`) fall back to the panel with a note in the tool result.
- **Wire.** The `artifact-ref` component's props gain `inline: true` and an optional `height` hint (an int; 80–1200 px). The artifact plugin's validator accepts them. An older console ignores them and shows the chip, which is the degrade path.
- **Shell embed mode.** The artifact shell gains a chrome-less embed mode (`/plugins/artifact/view?embed=<id>&v=<version>`). It renders exactly one version through the **same** frame builder as the panel: nonce CSP, vendored LIB map, theme tokens, loader lockdown and render-verdict reporting. There is no second frame builder.
- **Console host.** `ArtifactRefChip` (`apps/web/src/artifacts/`) renders an inline ref as an embedded shell iframe. It uses the bearer/theme handshake `PluginView` already uses (`apps/web/src/app/PluginView.tsx`), **extracted into a shared module** rather than copied. The frame reports its content height. The host clamps it to **[80, 1200] px**, and anything taller scrolls inside the frame. The header shows the title, the version and an **Open in panel** button.
- **Cost bounds.**
  - Inline frames **mount lazily** when scrolled near the viewport (IntersectionObserver).
  - At most **6** live inline frames exist per chat view. An older frame that scrolls away is replaced by a static "Click to resume" card, which remembers its last height so the layout doesn't jump.
  - Hydration and reattach render inline refs the same way. History replays the stored version, and a deleted artifact or a disabled plugin degrades to today's inert chip.
- **Live-only side effects stay live-only.** The chip's `onLive` hook (it opens the panel today) does **not** fire for an inline ref, because the answer is already in view.

### D3 — Streamed preview: opt-in streaming of one string argument

**Server: `stream_args`.** A tool can declare one string argument to stream, with tool metadata `{"stream_args": "code"}`. When the model streams `tool_call_chunks` for such a tool, `server/turn_stream.py` does the following:

1. It accumulates the raw arg JSON per tool-call id.
2. It **extracts the named string value incrementally on the server**: a small partial-JSON string scanner that handles escapes and stops at an unterminated escape. The console never parses partial JSON.
3. It emits `("tool_args", {id, arg, offset, chunk, done})` frames. Flushing is time-or-size, at ≤4 Hz or every 2 KB, whichever comes first, plus a final `done: true`.

Streaming stops silently once the value passes the artifact size cap. The executor relays these frames as a new optional DataPart, **`application/vnd.protolabs.tool-args-v1+json`**, on WORKING frames. Previews are live-only: they are not persisted to the task history and are not replayed on hydration or reattach. Once the tool has run, the finished artifact replaces the preview. The artifact plugin marks `show_artifact`, `update_artifact` and `rewrite_artifact` as `stream_args: "code"`.

**Console preview.** `a2aStream.ts` decodes `tool-args-v1` into a per-tool-call buffer. For a streaming `show_artifact` with inline placement, the chat renders the inline card at once:

- **Placeholder.** It shows the title as soon as it is known, plus a kind-aware status line.
- **html/svg preview.** For `html` and `svg`, the card shows a **preview frame**. The fence:
  - It uses `sandbox="allow-scripts"` with no same-origin.
  - Its CSP is `default-src 'none'; script-src 'nonce-<n>'; style-src 'unsafe-inline'; img-src data: blob:`. The one script allowed is the host's own nonce'd morph script, which is the only script carrying the nonce.
  - Before any markup reaches the preview frame, the partial markup is processed: an incomplete trailing tag is dropped, `<script>`, `<head>` and incomplete `<style>` blocks are stripped, and complete `<style>` blocks are hoisted. This is OIU's `processPartialHtml` pipeline (MIT; attribution in the file header).
  - Inline event handlers are inert, because the CSP has no `'unsafe-inline'` for scripts.
  - Model-written code therefore **never executes** in the preview.
- **Gating.** The preview stays hidden until the first `<style>` block closes, or until 1.5 KB of body markup has arrived without one. This is OIU's "never unstyled" rule.
- **Updates.** Updates are DOM-morphed (Idiomorph, vendored) so they don't flicker. They are throttled to 1 per second, with an immediate flush on milestones: the first style block closes, the body first appears, `done`.
- **Other kinds.** `react`, `mermaid` and `vega-lite` show the placeholder only, because a partial program or spec has nothing safe to preview.
- **Handover.** When the tool ends, the preview frame is swapped for the real inline frame (D2), keeping the last measured height so the layout doesn't jump.

### D4 — The send-to-chat bridge: UI starts a visible turn, only on a user gesture

Inside every artifact frame, inline or panel, the shell's shim adds two calls next to the existing `ask`:

- **`window.protoArtifact.send(text)`** puts `text` into the chat **as a user message** and starts a normal turn.
  - **Where it goes.** An inline frame sends to the session it is rendered in. The panel sends to the active chat tab.
  - **Gates.** Every hop checks the request: shim → shell → console host. The console host enforces all of these:
    - **A user gesture.** The host checks its *own* `navigator.userActivation.isActive`, because User Activation v2 propagates a child frame's activation to its ancestors. A frame can't fake that by posting a message directly. Where the API is missing, the host asks first with an inline "Send '…' to chat?" confirmation.
    - **Length.** 1–4000 characters.
    - **Busy.** Not while the target session has a turn running. The call is rejected with "the agent is busy" and the frame shows it.
    - **Rate.** At most 1 send per 2 s per frame.
  - **Visibility.** The message is posted with origin metadata `{via: "artifact", artifact_id, version}`. It shows the user's text with a small "from *‹title›*" label and is audited as a user turn. Nothing is hidden: the operator sees exactly what was sent.
  - **Default.** **On by default**, unlike `ask`. It costs exactly what typing costs and is just as visible. `ask` (a bare, invisible completion) stays opt-in and unchanged.
- **`window.protoArtifact.openLink(url)`** opens the link in a new tab with `noopener,noreferrer`, through the host. It accepts `https:` only, and an operator setting `artifact.open_link_origins` can narrow that to a list of origins.
- **Explicit values.** Frame control state is **not** synced to the agent. A UI that wants the agent to see a selection must put it in the `send` text. The skill teaches this (D7).

### D5 — Frame-rendered plugin components: kinds render without a console rebuild

- **Plugin declares a frame.** `registry.register_component(name, validator, frame="component.html")` declares that a plugin kind renders in a **plugin-served page**. The path is relative to the plugin's route prefix, and the manifest must list it under `public_paths`, because an opaque-origin frame sends no bearer.
- **Catalog.** The loader carries the frame path, and a new `GET /api/components` lists the live kinds, each as `{name, plugin, frame_url | null}`.
- **Console resolution order.** It becomes: `registerChatComponent` (TS registry) → **frame** → `[unsupported component]`. A compiled renderer still wins, so first-party and fork kinds are unchanged.
- **The frame host:**
  - It uses `sandbox="allow-scripts"` with no same-origin, and it never receives the bearer.
  - On load it gets the validated props and the theme tokens over `postMessage` (`{type: "protoComponent:init", props, theme}`). Props stay **data only, validated server-side** by the plugin's validator, exactly as today.
  - It autosizes with the D2 clamp and lazy-mount rules.
  - It gets the D4 `send` and `openLink` bridge, with the same gates.
- **DS plugin kit.** The plugin kit CSS and JS (`/_ds/plugin-kit.*`) are served publicly, so a frame looks native without a build step.
- **Limit.** A frame component can't call the plugin's gated routes. Anything it needs comes in its props, or through `send`. A plugin that needs a live, authenticated UI uses a rail view (ADR 0038/0045) or an artifact.

### D6 — Libraries and theme: vendored and token-driven, never CDN

Inline frames, previews and frame components get only what ADR 0116 D5 set up:

- **Libraries.** Vendored, SRI-pinned libraries served same-origin from the artifact vendor route.
- **Theme.** The `--pl-*` tokens: `--pl-color-chart-series1…8`, axis/grid, fg/bg and fonts. Frames **re-theme live** when the console theme changes.

There is no import map pointing at a CDN, and no new library is added by this ADR beyond **Idiomorph** (0BSD, for D3's preview). Adding three.js, d3 or gsap to the LIB map is a separate, per-library decision. Its cost is vendored bytes, its benefit what the skill can then promise. The ADR records OIU's set as the obvious candidates.

### D7 — Choosing the presentation is a skill, not a router (for now)

**Skill.** The artifact plugin's `rendering-artifacts` skill gains:

- **A presentation ladder** (D1's table), with "prefer the lowest tier that answers the request" and "inline for an answer, panel for a work product".
- **The html authoring order** that makes D3's preview good: `<style>` first, then markup that reads well before any script runs, then scripts last. This is OIU's ordered-field contract as a convention.
- **OIU's quality bar for interactive answers:**
  - every enabled control does real work;
  - labels are connected to inputs, with keyboard operation and visible focus;
  - numeric input is validated (`valueAsNumber`, `Number.isFinite`, domain bounds, no zero divisors), and NaN, Infinity or a stale result never shows as an answer;
  - units and assumptions are shown;
  - reduced motion is respected, with pause and reset for animation;
  - sample, user-provided, retrieved and calculated values are labelled;
  - `send` is used only from a labelled button and carries the selected values explicitly.

**Router (deferred).** A routing middleware is **deferred**. Revisit it if the eval set from slice S9 shows the model choosing the wrong tier often enough to matter. If built, it must use the agent's own gateway (no external classifier), fail open to "all tiers available" rather than failing the turn, and add no latency to text-only turns.

### D8 — Provenance on data: `table.source`

The core `table` component gains an optional `source` prop: a string of at most 200 characters, rendered as a muted caption ("Source: …"). It is optional, so existing payloads stay valid. The `show_component` docstring and the skill ask for it whenever the rows aren't the user's own input. This is OIU's required-provenance Table rule, made optional for backward compatibility.

## Consequences

- **G1–G5 close on existing seams.** A2A clients that ignore the new DataPart, the new props and `/api/components` see exactly what they see today.
- **Inline UI depends on the artifact plugin.** With the plugin off, `placement` doesn't exist and the model has tiers 1–2 only, which is today's floor.
- **`tool-args-v1` is new wire surface,** live-only and capped. It also starts to close ADR 0111 D3: the ACP shim can later use it for streaming edit diffs.
- **The `send` bridge is a new way to start turns.** Its gates are enforced in the host, not in untrusted frame code, and every send is visible and audited as a user turn. The threat it adds is "a frame makes the user's click send something they didn't read". The visible label and the gesture requirement bound it, and HITL-gated tools still park as usual.
- **Frame components trade capability for portability.** They get no bearer, props only and a public page. That is the intended boundary.
- **Performance.** Lazy mounting plus the 6-live-frame cap bounds memory on long transcripts. The preview throttle bounds CPU while streaming.
- **OIU code taken under MIT** (`processPartialHtml`, the throttle and flush rules, the height clamp, and the authoring guidance) is attributed in file headers.

## Slices

Each slice is independently mergeable into the epic branch. Dependencies are noted in brackets.

| # | Slice | Area |
|---|---|---|
| S1 | `table.source` prop: validator, renderer, docstring, tests (D8) | core `graph/components.py`, `ChatComponent.tsx` |
| S2 | `stream_args` tool metadata + server partial-string extraction + `tool_args` frames + `tool-args-v1` DataPart (D3, server) | `server/turn_stream.py`, `a2a_impl/executor.py`, `docs/reference/extensions.md` |
| S3 | Console decode of `tool-args-v1` into a per-tool-call buffer [S2] | `apps/web/src/lib/api/a2aStream.ts`, chat store |
| S4 | Artifact `placement` param + `artifact-ref` `inline`/`height` props + `stream_args` marks on the three write tools (D2/D3, plugin side) | `plugins/artifact/_tools.py`, `_ref.py` |
| S5 | Artifact shell embed mode (D2) | `plugins/artifact/shell.js`, `shell.html`, `_routes.py` |
| S6 | Extract the PluginView bearer/theme handshake into a shared module (no behavior change) | `apps/web/src/app/PluginView.tsx` + new module |
| S7 | Inline host in `ArtifactRefChip`: embedded frame, height clamp, lazy mount, 6-frame cap, Open in panel [S4, S5, S6] | `apps/web/src/artifacts/` |
| S8 | Streamed preview frame: partial-HTML processing, gating, Idiomorph vendoring, throttle, swap to the final frame [S3, S7] | `apps/web/src/artifacts/` |
| S9 | `rendering-artifacts` skill: ladder, authoring order, quality bar, plus a small eval prompt set (D7) | `plugins/artifact/skills/` |
| S10 | `send` / `openLink` bridge: shell shim + console host gates + origin-tagged user message (D4) [S7] | `plugins/artifact/shell.js`, console chat send path |
| S11 | `register_component(…, frame=)` + loader + `GET /api/components` (D5, server) | `graph/plugins/registry.py`, loader, a route |
| S12 | Console frame-component host + resolution order + bridge reuse [S6, S10, S11] | `apps/web/src/chat/ChatComponent.tsx`, `src/ext/componentRegistry.ts` |

## Open questions (operator calls)

1. **Inline by default?** Should `show_artifact` default to `inline` for small html/svg/vega-lite artifacts, or stay `panel` and let the skill choose? This ADR says `panel` stays the default.
2. **Live-frame cap.** Is 6 the right default, and should it be a setting?
3. **`send` on by default.** This ADR argues yes, because the gesture is required and the turn is visible. Is that the call?
4. **Libraries.** Which, if any, of three.js, d3 and gsap should join the vendored LIB map?
