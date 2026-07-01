# 0061 — Frontend extension registries (fork-safe console behavior seams)

Status: **Accepted** (slash-command, composer-action, palette-command registries + UI-state slices shipped)

## Context

The **backend** is fork-safe: a fork adds tools, middleware, routes, subagents, goal
verifiers, and chat-commands through `register_*` seams on the `PluginRegistry`
(`graph/plugins/registry.py`) **without editing core**, so `git pull` from upstream
never conflicts. The chat-command seam (ADR / PR #1334, `register_chat_command`) is the
most recent: a plugin owns `/<name>` and the core dispatcher consults the registry, no
core edit.

The **frontend has no equivalent for behavior**. The console's view layer *is* fork-safe
— a fork drops `src/ext/<name>.tsx` calling `registerSurface()` (ADR 0038 D3), and plugin
manifest `views` render as sandboxed iframes (`placement`/`utility`/`palette`/`slot`, ADR
0026/0057) — all without core edits. But anything that isn't a view-shaped iframe is
hardcoded. The GitHub→plugin extraction (PR #1336, issue #1337) made this concrete:
GitHub was wired straight into `apps/web/src/chat/ChatSurface.tsx` (a `verb === "issue"`
branch that opened a dialog) and `apps/web/src/state/uiStore.ts` (`newIssue*` state). A
fork that wants its own chat-input behavior must patch those core files — a permanent
merge-conflict surface on every update.

Concretely, today a fork **cannot** without editing core:
- add a **client-side slash command** or **intercept `/x`** to do something other than
  send (`ChatSurface.runClientSlash` was a closed `switch`; `completeCommand` had no hook);
- add a **composer action** button (the PromptInput actions slot is hardcoded);
- add a **root command-palette command** (`usePaletteRegistry.deepLinkCommands()` is a
  static list);
- add **UI-store state** (`uiStore` is a closed `UIState`, no slice system).

## Decision

Give the console the same *extend-without-editing-core, update-safe* property the backend
has, by extending the existing `src/ext/` seam (ADR 0038 D3) with **behavior registries**
that mirror `registerSurface`: static registration at module load, **first-registration-
wins (HMR-safe)**, fork modules live only under `src/ext/` so upstream never touches them.

**Core dogfoods every registry** — its own behavior registers through the same seam, with
no bypass, exactly like the backend `register_*` (there is no "core slash command" special
case). That guarantees the seam is real: if it works for core, it works for a fork.

### This ADR's first seam — the slash-command registry (shipped)

`apps/web/src/ext/slashRegistry.ts`:

```ts
registerSlashCommand({
  name,                 // the /<name> token (case-insensitive)
  description,          // shown in the slash menu
  usage?,
  run: (ctx: SlashContext) => boolean,  // true ⇒ handled (send short-circuited, draft cleared);
})                                      // false ⇒ fall through (insert "/name " to edit + send)
```

`SlashContext = { rest, sessionId, noteToThread, setDraft, focusComposer }` — the host
(`ChatSurface`) builds it from local state + the chat store when the command fires.
**Registering a token CLAIMS it** — the frontend twin of `register_chat_command`. Distinct
from **server** slash commands (`/api/chat/commands`, e.g. `/goal`, plugin `/issue`), which
fill the draft for the user to send; client commands act locally on pick/submit.

Core's `/new`, `/clear`, `/effort` moved out of the hardcoded `runClientSlash` switch into
`chat/coreSlashCommands.ts`, registered through this seam. `ChatSurface` builds the slash
menu from `registeredSlashCommands()` + the server list, and `runClientSlash` dispatches via
`findSlashCommand` — no hardcoded verbs remain.

### The other two seams (also shipped, same pattern)

- **`registerComposerAction`** (`apps/web/src/ext/composerRegistry.ts`) — adds a control to
  the chat composer's actions slot (beside the model picker). `ChatSurface` renders
  `registeredComposerActions()` there. An **additive** seam: core's composer controls
  (attach, model select, send) are DS `PromptInput` built-ins, not migrated; the registry is
  purely for fork-added actions (e.g. a templates or voice button). Handler context:
  `{ sessionId, setDraft, focusComposer, noteToThread }`.
- **`registerPaletteCommand`** (`apps/web/src/ext/paletteRegistry.ts`) — adds a root ⌘K
  command in the "Commands" group; `usePaletteRegistry` maps these onto DS palette `Command`s.
  **Dogfooded:** core's deep-links (Plugins: Discover, Settings, Settings: Fleet/Telemetry)
  register through this seam, so the registry is the only path (no `deepLinkCommands()`
  bypass). Handler context: `{ close }`. (Distinct from plugin manifest `palette` views,
  ADR 0057, which morph the palette body into a plugin iframe — these RUN trusted in-process
  code.)
- **`registerKeybinding`** (`apps/web/src/ext/keybindingRegistry.ts`, ADR 0063) — binds a
  default keyboard shortcut (optionally focus-scoped to a `data-kb-scope` panel). Every
  registered binding auto-appears in **Settings ▸ Keyboard** (user-rebindable, with conflict
  detection) and fires through the one global keydown host. **Dogfooded:** core's own shortcuts
  (`keybindings/coreKeybindings.ts`) register through this same seam. Re-exported from
  `src/ext/index.ts` alongside the seams above (#1457) so a fork reaches it the same way.

### UI-state slices (shipped, `createUISlice`)

- **`createUISlice(namespace, initial)`** (`apps/web/src/ext/uiStateRegistry.ts`) — a fork
  owns a namespaced, **persisted** zustand store for its own UI state. It deliberately does
  **not** merge into the core `UIState` object (zustand has no runtime slice-merge, and a
  fork's state doesn't belong in core's closed shape) — it gives the fork its OWN store,
  *standardized*: the same per-agent persistence as core layout (ADR 0042) and first-wins per
  namespace (re-calling returns the same store, HMR-safe). Used like any zustand hook
  (`const useX = createUISlice("ns", {…}); useX((s) => s.field); useX.setState(…)`). Core
  UI/layout state stays in `state/uiStore.ts` — it's core's, not a fork slice.

## Consequences

- A fork adds chat-input behavior, composer/palette actions, and its own persisted UI-state
  by adding a `src/ext/` module — no core edits, no upstream merge conflicts. Same story as
  the backend's `register_*`.
- The seam is **build-time + trusted/in-process** (the fork compiles its own bundle), NOT the
  sandboxed-iframe plugin path (ADR 0026). Untrusted UI still goes through plugin iframe views.
- Core behavior is now defined through the public seam, so the registry can't silently rot —
  if core's `/new` works, a fork's command does too.

## Alternatives considered

- **A runtime (plugin-manifest) slash seam** like iframe views — rejected: client slash
  behavior is trusted in-process code, not a sandboxed page; it belongs on the `src/ext/`
  (fork, build-time) path, mirroring `registerSurface`.
- **Leave it hardcoded, document the patch points** — rejected: that's exactly the
  merge-conflict surface this ADR removes, and it contradicts the backend's fork-safety.
