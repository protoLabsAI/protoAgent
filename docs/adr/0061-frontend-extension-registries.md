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

`SlashContext = { rest, sessionId, noteToThread, setDraft, focusComposer, flagOn?,
serverCommands? }` — the host (`ChatSurface`) builds it from local state + the chat store
when the command fires. The two optional fields carry the host's developer-flag predicate
(ADR 0068) and its fetched server-command list, so a registry-enumerating command (core's
`/help`, #1700) reflects the host's own visibility rules and what's actually installed
instead of a hardcoded copy.
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
- **`registerPaletteCommand`** (`apps/web/src/ext/paletteRegistry.ts`) — adds a root command-palette
  command in the "Commands" group; `usePaletteRegistry` maps these onto DS palette `Command`s.
  **Dogfooded:** core's deep-links (Plugins: Discover, Settings, Settings: Fleet/Telemetry)
  register through this seam, so the registry is the only path (no `deepLinkCommands()`
  bypass). Handler context: `{ close }`. (Distinct from plugin manifest `palette` views,
  ADR 0057, which morph the palette body into a plugin iframe — these RUN trusted in-process
  code.)

  A command carries presentation alongside behavior — `icon`, `hint`, `disabled`, and a
  `keybinding` id. The presentation fields are the DS `Command` fields (`toDsCommand` passes
  them straight through); the seam adds no rendering vocabulary of its own. Two deliberate
  non-fields: a `disabledReason`, because a disabled row's reason belongs in the `hint` it
  already has (core's Fleet Room command does exactly that), and a display-only `shortcut`
  *string*, because bindings are user-rebindable — Settings ▸ Keyboard persists an override,
  so a literal `"⌘⇧K"` starts lying the moment the operator rebinds it. `keybinding` names a
  `registerKeybinding` id instead (it binds nothing — the combo still fires through the
  keybinding host) and the adapter renders `formatCombo(effectiveCombo(binding))`, i.e. the
  live combo, into the row's hint slot. Unlike the other registries this one is
  **last-write-wins by id** (matching `registerKeybinding`) and returns a **guarded
  unregister** — it removes the command only while that command is still the registered one,
  so a stale closure can't evict a newer registration. A re-registered id keeps its original
  display position.

  Two additions make it usable beyond a fixed deep-link:

  - **Declarative gating — `flag?: string` + `hostOnly?: boolean`, read through
    `visiblePaletteCommands(flagOn, onHost)`.** Same two axes, same shape as the settings
    sections' `visibleSections` (`settings/sectionGate.ts`), and the same `flag` contract as
    `registerSlashCommand` — one gating vocabulary for the console. Registration stays
    **unconditional** and the filter runs at **read** time, which is the load-bearing part:
    registration happens once at module load, before `/api/flags` has answered, and
    `useFlagPredicate` fails **closed** while that request is in flight (ADR 0068), so a gate
    resolved at registration would hide a flag-gated row *permanently*. Re-filtering per
    render lets the late-arriving flag flip the row on. Gates are **data, not a predicate**:
    they cannot throw inside the root render, cannot get expensive, and anything holding the
    two axes can answer them (a "why is this hidden?" affordance included). `hostOnly` is the
    URL-slug axis (`isHostConsole()`), *not* the fleet-nesting one — a sister agent's slug
    window drives the hub's fleet and keeps fleet rows. A row that should stay visible but
    dead uses `disabled` + `hint` instead.
  - **`registerPaletteSource(fn: () => PaletteCommand[])`** contributes commands computed at
    **read** time rather than registration time, for rows that track live data (open chat
    tabs, a roster) — and, since a source picks its rows per read, it is also the escape
    hatch for a condition the two gate axes can't express, with the failure contained in the
    registry instead of in the root render. Same guarded-unregister contract; a broken source
    is skipped (its rows, not the sources after it) rather than blanking the palette, and
    "broken" means a `throw` **or** a return that isn't an array — the seam is a build-time
    edge, so an `async` source (a Promise), an id-keyed object or a `false` all typecheck at
    the fork's call site and would otherwise throw `rows is not iterable` past the host's
    effect and onto the console's ROOT error boundary, trading one bad row for the whole app.
    Statics win an id collision over sources; the first source to claim an id wins over later
    ones, in the narrowed reads as well as the whole one.

    **Read time is the HOST's read, and the two halves therefore reach the palette by
    different paths.** Nothing observes a source's underlying data — `paletteCommandsVersion()`
    moves only on register/unregister — so the host cannot key an effect on it, and the DS's
    static path stores the array it is handed verbatim (`getStaticCommands()` flattens the
    registered groups). Snapshot a source's rows into `registerCommands` and they freeze at
    whichever effect run registered them: ⌘K keeps listing the tab the operator closed and
    never lists the one they just opened, until some unrelated change (a plugin toggle, a
    roster edit, a rebind) happens to re-run the effect. So `visiblePaletteCommands(flagOn,
    onHost, from)` reads the halves apart: `"static"` rows are snapshotted (a fixed list is
    correct to freeze, and it keeps them in their registered display position), while
    `"dynamic"` rows are served by a DS **`CommandProvider`** the commands view re-invokes on
    every open and every keystroke. The provider is wired only while a source exists — the DS
    shows its "Searching…" affordance whenever any provider declares `getCommands`, so an
    always-empty one would charge every keystroke for nothing, in every window that mounts the
    palette (the desktop launcher included) — and it applies the query itself, because the DS
    client-filters only statics
    (a provider is normally a remote search that already applied it). The honest contract is
    therefore "re-read on every palette read", not "re-rendered when your data changes": a
    row that changes while the palette sits open and untouched appears at the next keystroke.

  Consumers observe the registry the way they observe the DS one: **`subscribePaletteCommands(fn)`**
  plus a monotonic **`paletteCommandsVersion()`** (bumped on every register/unregister) is exactly
  the `useSyncExternalStore` pair, so a command registered *after* the root view's first render
  still appears — the pre-existing read-once-in-an-effect shape could never show it.
  `usePaletteRegistry` consumes all of it (gate, presentation, both halves of the read, version),
  so no part of the seam ships as API nothing reads.
- **`registerKeybinding`** (`apps/web/src/ext/keybindingRegistry.ts`, ADR 0063) — binds a
  default keyboard shortcut (optionally focus-scoped to a `data-kb-scope` panel). Every
  registered binding auto-appears in **Settings ▸ Keyboard** (user-rebindable, with conflict
  detection) and fires through the one global keydown host. **Dogfooded:** core's own shortcuts
  (`keybindings/coreKeybindings.ts`) register through this same seam. Re-exported from
  `src/ext/index.ts` alongside the seams above (#1457) so a fork reaches it the same way.

### Dispatching a client command from OUTSIDE the composer (`slashDispatch`, internal)

`runClientSlash` lives inside `ChatSessionSlot` and closes over per-slot React state (the
draft setter, the composer-form opener, the fetched server command list, the developer-flag
predicate, `noteToThread`), so no other surface can build a `SlashContext` — and a caller
must never synthesize one: a no-op `noteToThread` would silently swallow the output of every
command that answers with a system note. So the **visible** slot publishes its dispatcher on
`apps/web/src/chat/slashDispatch.ts` — module-level, last-write-wins, guarded unregister, the
same imperative-seam shape (and the same directory) as `chat/escapeStop.ts`, which solves
this for the Escape keybinding. `runSlashFromOutside(raw)` (leading slash optional) returns
`false` when nothing handled it, and `slashDispatchTarget()` reports whether a slot is
mounted at all, whether it has a session, and whether the chat surface is actually on screen.

All three facts are load-bearing, because outside the composer a decline is *silent* — there
is no draft for the token to fall through into. `null` does **not** mean "the operator
navigated away": the built-in chat slot is mounted for the app's lifetime (#613) and stays
registered across rail switches, which is what lets the palette reach chat from any surface. It means
there is no built-in chat slot in this window at all — the frameless desktop launcher (ADR
0057), or a fork surface / plugin iframe holding the `chat` slot ahead of the built-in one.
And a `sessionId` of `null` disqualifies the whole set, not just part of it: 13 of the 16
core commands `return false`, while `/goal` and `/watch` return `true` and answer through
`noteToThread`, which the host itself no-ops without a session. Only `/new` does real work.
So the caller's rule is "create or focus a session first", not "allowlist the three that
return true". `coreSlashCommands.test.ts` pins that inventory so this paragraph can't drift.

`surfaceActive: false` is the same hazard on the visibility axis: the chat surface renders
under `display: none` when another rail surface is active, so a command that answers through
`noteToThread` — or opens the `/effort` picker in the composer panel — runs, returns `true`,
and shows the operator nothing. The seam reports rather than vetoes (firing `/clear` or
`/bypass` without yanking the operator onto the chat rail is legitimate); a caller that
dispatches something with visible output raises the surface first via `openView("chat")`.

**Deliberately NOT a fork registry, and NOT re-exported from `src/ext/index.ts`.** It is a
host-internal bridge between two core surfaces (the command palette and the chat composer), not
an extension point: a fork adds a command with `registerSlashCommand` and gets palette
dispatch for free. `escapeStop` sets that precedent — same shape, same non-export.

The seam also carries **`prefillDraft` / `prefillChatDraft(text)`** — "put this at the front
of the composer and hand the operator the caret", the action for a token that *cannot* be run
from outside. It rides the same registration rather than a parallel seam because it needs the
same live closure: the draft is `useState` inside `ChatSessionSlot` (seeded from
sessionStorage on mount), so a write from outside React is swallowed by the mounted slot. It
**prefixes rather than replaces**: a `/token` leads the message anyway, so prepending is both
the correct placement and the non-destructive one — the same rule the composer's own
`/`-menu completion follows (it swaps only the token under the caret and keeps the rest).

### The chat's verbs in the command palette (`app/chatSlashPalette.ts`)

The palette's first consumer of both seams (#3292): every client slash command and every
server **user-facing skill** as palette rows, so the console's real verbs are reachable from
the one surface an operator asks "how do I do X?".

**A skill is not runnable, and its row must not pretend otherwise.** A `user_facing` skill
(ADR 0052) is a message *rewrite* the server applies on the next **send** — `_skill_directive`
injects the procedure and falls through to an ordinary lead-agent turn. There is no endpoint
that runs one, and the palette must not send a message on the operator's behalf. So a skill
row prefills `/<skill> ` into the composer, raises chat, and stops.

Generalised: **a row either RUNS or DRAFTS, and every drafting row says the same thing**
("drafts in chat — you send it"), so one phrase means one behaviour. Every skill drafts;
`/btw` does too, because it takes a question the palette has no way to ask for and running it
bare only prints its own usage note; and so do the two per-tab modes, for a safety reason
spelled out below. And a row dispatches the token it actually *claims* —
`/goal`'s row runs `goal new`, the one branch the client command owns (bare `/goal` falls
through to the server control command and would return `false`), and is labelled
`/goal new · Open the guided goal form`: the registry description ("Set or check goals — …")
leads with two branches this row does not run, and a row must not lead with a verb it can't
deliver.

**The row's two slots: label = `/token · what it does`, hint = the caveat.** The label is the
composer `/` menu's own shape (token, then description), which is what makes this read as the
same list in a second place. The description cannot live in the hint, because the hint is
occupied exactly when the prose is needed most: with no chat open every client row's hint is a
reason, and a skill row's is always its draft promise — leaving a column of bare tokens
(`/perf`, `/btw`, and skills named by whoever authored them) that an operator cannot shop
from. Leaving `hint` unset is also what lets `toDsCommand` render the row's live keybinding
combo, so a row that advertises one says nothing else.

**Session semantics are decided per command, on one question: does it need THIS chat — its
content or its per-tab mode — or merely A thread?** Outside the composer a `return false` is a
silent no-op — there is no draft for the token to fall into — so a row that would decline
must not look runnable:

- **Needs this chat** (`/clear /export /publish /btw /trajectory /prompt /perf /compact
  /bypass /incognito`) — the row is `disabled` with the reason in `hint`. The first eight read
  or rewrite accumulated history, and auto-creating a blank tab to export an empty transcript
  or compact nothing would "succeed" and tell the operator nothing; the last two are per-tab
  modes whose row exists to name the current one. Disabled-and-explaining keeps it
  discoverable.
- **Needs only a thread** (`/help /effort /model /goal /watch`) — a place to print, or a tab
  to configure before typing into it. The row creates or focuses one first
  (`chatStore.createSession` reuses a pristine blank), then dispatches.
- **Needs nothing** — `/new`, with one gate: `createSession` *reuses* a pristine blank rather
  than making a second one, so when that blank **is** the current tab the dispatch is a
  genuine no-op. The row disables itself on exactly the condition every other "new chat"
  affordance already does (`unusedSession`, MobileShell/SessionSheet).

The gate is "is there a session", never "does it have messages": the palette must not invent
a stricter rule than the composer, where `/export` on an empty tab is allowed.

**A per-tab MODE (`/bypass`, `/incognito`) is never a one-Enter row.** Dispatched bare, both
*toggle* (`next = arg === "on" ? true : arg === "off" ? false : !cur`). In the composer that
is fine — the operator typed the whole token, at a tab whose current mode is on screen. From a
fuzzy search it is not: the palette preselects the first match and Enter runs it, so "yolo" +
Enter would arm `run_command` auto-approval — **a trust boundary** — in a direction the row's
label never named. A directional *pair* (`/bypass on` + `/bypass off`) does not fix it either,
because the DS matcher is a case-insensitive **substring** test over the row's whole
label/hint/keywords, and "on" is a substring of half the English in them — which of the pair a
query preselects is not something the row builder can guarantee. So a mode row **drafts**: it
raises chat, types `/bypass ` into the composer, and stops. The operator supplies the
direction and the send, on the tab it applies to, and reads the command's own system note
afterwards. The row still earns its place, because its **label carries the mode's current
value** (`… — now off`) — the thing an operator opens ⌘⇧K to find out, and the reason both
modes are `needsThisChat`.

**Rows go through the STATIC path, not `registerPaletteSource`.** They were a source first,
for two real reasons — the skill list is live server state, and a client row's `disabled`
tracks the session — and a source was still wrong, on three counts that are all about the
PATH rather than the data:

- **Ranking.** A provider's rows are *ordered*, never ranked against the corpus
  (`orderCommands` after `rankCommands`, palette/rootView.tsx), because a source is a remote
  search that applied the query its own way. So `/clear` under the query "clear" would sit
  below every static and every surface. With ~80 commands arriving across the sibling
  command PRs, ranking is what keeps any of them findable.
- **Cost, in windows that get nothing back.** Declaring `getCommands` puts a 120ms debounce
  and a "Searching…" spinner in front of every keystroke in *every* window that mounts the
  palette — the frameless launcher included, which mounts no chat and can never be served one
  of these rows.
- **Staleness**, which is what made it urgent. A provider's results outlive the query they
  answered: the loop only overwrites them when a read *resolves*. Our root view now stamps
  them with their query and drops a stale stamp (`rootView.tsx`), but the DS's own
  `CommandsBody` still does not (protoContent#504) — and a row that RUNS something should not
  depend on that being fixed wherever it might be rendered.

Statics are client-filtered and ranked synchronously instead, and the host keeps them live by
**re-registering**: it subscribes to the chat store through a string projection of everything
a row renders from (`chatPaletteSignature()` — so a streamed token doesn't churn the group)
and to the shared `/api/chat/commands` query for the skills. Same liveness, ranked with
everything else. **Core therefore still ships zero sources**, which is what keeps
`hasPaletteSources()` meaningful.

**Which windows get the rows is a fact about the WINDOW, not about what is mounted.** The gate
is `chatSlotProvider(...) === "builtin"` (app/ChatSlot — the same resolution `ChatSlot`
renders with), passed in by App. It is *not* `slashDispatchTarget() !== null`: the DS AppShell
unmounts a collapsed dock, so that seam goes `null` on the one-click "Hide left panel"
gesture, and gating on it emptied the whole Chat + Skills group out of ⌘⇧K in exactly the
state the palette is most useful in. Running a row from there still works unchanged — the
raise (`navigate({kind:"view",id:"chat"})` → `openView`) un-collapses the dock, and the
handoff poll waits for the remounted slot to re-register.

Every navigation goes through the palette's serializable `NavIntent` chokepoint, injected
rather than imported: a direct `useUI.getState()` call is an inert no-op in the frameless
launcher's shell-less context. (The launcher gets no rows at all — it mounts no ChatSurface,
so its `builtInChat` is false and it also never pays for a source it could not serve.)

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
