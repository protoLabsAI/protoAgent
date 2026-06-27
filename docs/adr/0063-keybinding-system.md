# ADR 0063 — Scoped, user-rebindable keybinding system

**Status:** Accepted (shipped)

## Context

Keyboard handling in the console was ad-hoc: ⌘K (the DS `usePaletteHotkey`), an Escape on the
AppDrawer, composer keys in `ChatSurface`, and a couple of Enter-to-submit handlers — all
hand-rolled, none discoverable, none rebindable. There was no registry, no persistence, no way
for a user to remap a shortcut or for a fork/plugin to add one. We also want shortcuts that
depend on **focus** — e.g. chat tab shortcuts that only fire when you're in the chat panel —
not just flat global hotkeys.

## Decision

A dedicated keybinding layer for **global app commands** (the rebindable surface; DS-internal
and contextual composer keys stay as-is), mirroring the established patterns (the `src/ext/`
registries — ADR 0061, the contextMenu store+host — ADR 0036, per-key persistence — ADR 0042).

- **`registerKeybinding({ id, label, group, defaultKeys, scope?, allowInInput?, run })`**
  (`src/ext/keybindingRegistry.ts`) — the fork/plugin seam, last-write-wins by id (HMR-safe).
- **One global keydown host** (`useGlobalKeybindings`, mounted in App) normalizes the event to a
  combo (`mod+k`, where `mod` = ⌘ on mac / Ctrl else), then runs the matching binding honoring:
  - **Focus scope** — a panel marks its root `data-kb-scope="<id>"` (the chat stage = `"chat"`);
    the host walks up from the focused element to build the active scope chain. A binding with a
    `scope` fires only when that scope is in the chain; **most-specific wins** (a panel binding
    beats a global one for the same combo) — so the same combo can mean different things in
    different panels.
  - **Typing gate** — plain-key bindings (`/`) fire only when not in an editable field;
    mod-combos opt into `allowInInput` to fire while typing (e.g. ⌃Tab, ⌘1).
  - **User overrides** — a GLOBAL `{ id → combo }` map persisted to `protoagent.keybindings`
    (not per-agent: a user's shortcuts are theirs everywhere).
- **Settings ▸ Keyboard** (`KeybindingsPanel`) lists every registered binding by group; click to
  record a new combo (the host is muted via a `capturing` intent while recording), with
  conflict detection (same combo in an overlapping scope is blocked), per-row + reset-all.
- **Core defaults dogfood the seam** (`coreKeybindings.ts`): `⌘K` palette (adopted off the DS
  `usePaletteHotkey` — palette open-state moved to an intents store), `⌘,` Settings, `/` focus
  composer (global); `⌘T` new, `⌘⇧K` clear, `⌃Tab`/`⌃⇧Tab` prev/next, `⌘1–9` jump (scope `"chat"`);
  global VS Code-style panel toggles `⌘B` left rail / `⌘⌥B` right panel / `⌘J` bottom dock; and
  panel-**focus** binds `⌃1` chat composer / `⌃2` left / `⌃3` right / `⌃4` bottom — these move
  keyboard focus *into* a dock (so its scoped binds activate), and use the **literal `⌃`** (the
  secondary modifier on mac) precisely so they don't collide with `⌘1–9` tab-jump. `⌃2/3/4` focus
  the first interactive element in the dock's AppShell column; `⌃1` reuses the composer-focus
  intent. (The combo layer maps `Ctrl`→`mod` on Win/Linux, so the literal-`⌃` default is
  mac-semantics — rebind elsewhere; the conflict detector flags the `⌘1`/`Ctrl+1` overlap there.)

## Consequences

- **Rebindable + discoverable** — every shortcut is listed and remappable; forks/plugins add
  bindings (and their own `data-kb-scope` panels) without touching core.
- **Focus-aware** — "only when I'm in the chat input" is just `scope: "chat"`; the model extends
  to any panel/plugin view.
- **Browser-reserved caveat (deliberate, verified)** — the chat defaults mirror the browser
  (`⌘T` new, `⌘1–9` jump, `⌃Tab` switch). A real browser tab reserves these (new tab / tab-switch)
  and swallows them before the page sees them, but the **Tauri desktop app does not**: its only
  custom menu is the tray (Show/Hide/Updates/Quit, `apps/desktop/src-tauri/src/lib.rs`), the
  default macOS app menu claims only standard accelerators (`⌘Z/X/C/V`, `⌘W/M/Q`) — none of
  `⌘T`/`⌘1–9`/`⌃Tab` — and a WKWebView has no tab/omnibox chrome to intercept them. So they work
  in the desktop app (the primary target); in the browser console they're rebindable to a free
  combo (e.g. the `⌘⌃` family). We chose the familiar combos + this limitation over uglier
  always-works chords. (Headless Playwright has no browser chrome, so e2e still exercises them.)
- **Untouched:** DS-internal keys (Dialog Esc, palette/menu/tab arrows, AppShell resize) and the
  composer's contextual slash-menu nav remain owned by their components — not global commands.

## References

- ADR 0061 (`src/ext/` fork registries — the seam pattern), ADR 0036 (context-menu store+host),
  ADR 0042 (persisted client state), ADR 0057 (command palette — ⌘K now a regular binding).
  Module: `apps/web/src/keybindings/` + `apps/web/src/ext/keybindingRegistry.ts`.
