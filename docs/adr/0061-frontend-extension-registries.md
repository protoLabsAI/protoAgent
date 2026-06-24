# 0061 — Frontend extension registries (fork-safe console behavior seams)

Status: **Accepted** (slash-command registry shipped; composer-action + palette-command registries planned)

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

### Planned (follow-on, same pattern)

- **`registerComposerAction`** — buttons in the PromptInput actions slot (closes the
  composer-action gap).
- **`registerPaletteCommand`** — root ⌘K commands (closes the `deepLinkCommands` gap).

### Out of scope (deferred)

- **UI-store slices.** Zustand has no runtime slice-merge, and a fork's `src/ext/` surface
  can own its own store for its own state, so the need is weak. Revisit only if a real case
  appears. (Issue #1337.)

## Consequences

- A fork adds chat-input behavior (and, once shipped, composer/palette actions) by adding a
  `src/ext/` module — no core edits, no upstream merge conflicts. Same story as the backend.
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
