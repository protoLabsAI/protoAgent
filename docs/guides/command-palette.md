# Command palette (⌘⇧K)

The console has a command palette — press **⌘⇧K** (macOS) / **Ctrl-Shift-K**
(Linux/Windows) to open it from anywhere. It's the fast path to jump between surfaces and
act without hunting through rails and menus ([ADR 0057](/adr/0057-command-palette)).

Note the shift: **plain ⌘K is _Clear conversation_** in chat, not the palette. Both chords
are rebindable in Settings ▸ Keyboard ([ADR 0063](/adr/0063-keybinding-system)).

**On the desktop app the palette also has its own window.** The quick launcher —
**⌥Space** (macOS) / **Ctrl+Alt+Space** — is this same palette, frameless and
always-on-top, summoned with the console hidden or another app focused; a "go to" hands
off to the main window, and it dismisses on Escape or blur. That one is an *OS-global*
hotkey owned by the desktop shell, so it is rebound in its own section of
Settings ▸ Keyboard, not with the in-app chords above.

## What's in it

- **Chat with _‹agent›_** — morphs the palette into a quick chat with this window's
  agent: one preserved thread, full streaming and tool cards, `/clear` to wipe it — handy
  for a one-off ask without leaving what you're doing.
- **Fleet Room** — the fleet as a room, opened inside the palette: a presence-aware
  roster of members (*this instance* · online · stopped · remote), the live fleet
  activity feed beside it, and a send bar below (Enter goes to the `@name` you addressed,
  or to everyone online when you addressed no one; ⌘↵ always broadcasts). A roster row
  carries that member's controls: click the name to **DM** it (the same quick chat,
  retargeted through the hub proxy), **open its full console**, or **start/stop** it —
  which is why every member's name is a keyword on this one command, so typing `ava`
  surfaces the room.
  Start/stop is offered only for a **local** member you aren't already looking at (never
  the host, a remote member, or the agent serving this window), and the command itself is
  disabled in the one place a fleet is a fleet-of-one: a spawned member reached directly
  on its own port, where it points you at the host instead.
  *(Per-member root commands — the old **Toggle Fleet Agent** picker and per-member
  quick-chat — folded into this room; they are one hop in now, not gone.)*
- **Plugin views** — each enabled plugin's views are their own group. A view can also opt
  to render *inside* the palette by declaring `palette: "inline"` on it (so a lightweight
  tool can live behind a keystroke instead of taking a rail slot).
- **Open…** — the built-in surfaces (Chat, Activity, Knowledge, Studio, Agent, Plugins,
  Settings, plus whatever a fork adds) live one hop in, behind **Open…**, so the root list
  stays short.
- **Deep links** — the jumps worth their own command: **Settings**, **Settings: Fleet**,
  **Settings: Telemetry**, **Plugins: Discover**, **Plugins: Install from URL**.
- **Knowledge** — type two or more characters and the palette searches the agent's
  knowledge store live ([ADR 0021](/adr/0021-agent-memory-architecture)) — findings,
  notes, the daily log, harvested sessions — and lists the top matches under a
  **Knowledge** heading. Each row is trailed by where that entry came from (its source
  file, or failing that its domain).
  Picking one opens the **Knowledge** surface with that same search already run — clearing
  any *pending review* filter it was left on — so the entry you chose is in the list you
  land on. (The palette can't scroll the surface to one entry: the surface has no
  per-entry anchor, so the search is what puts your pick in front of you.)
  Matching is **by word, and by the start of the word you are still typing** — `postg`
  finds *Postgres tuning*, and once you finish a word the next one you start is the one
  being completed. That is not free: the store's keyword index matches whole words, so the
  palette asks it for a prefix term on the last word specifically because a type-ahead that
  went whole-word only would show you nothing for every character before the end of each
  word — a blank list that reads as "no matches" when it means "keep typing".
  Four things are deliberate here. The rows appear only on an instance that **has** a
  knowledge store (`knowledge.enabled` in **Settings ▸ System ▸ Runtime**); where there is
  no store there is no search and the palette does not offer one. It searches only once you
  have typed something: an empty box would otherwise list the most recent entries in the
  store, burying the commands. It shows a handful of matches rather than everything that
  matched, and when there are more it adds a last **All matches in Knowledge** row that
  takes you to the surface on the same search — so the shortlist is never a dead end. And
  when the palette cannot complete the search — the store unreachable, the bearer rejected,
  the request past its deadline — it says **Knowledge search unavailable** with the reason,
  rather than quietly showing nothing, which would be indistinguishable from "no matches".
  (A search the store itself errors on is the exception: that route answers `200` with an
  empty list, so it does read as "no matches" — check the agent log if a term you know is
  there returns nothing.)

Groups render in registration order — **Agents**, then **Plugins**, then **Commands** —
so the agent and its fleet stay at the top. Live search results (Knowledge) arrive after
them: they are fetched per keystroke rather than registered up front.

## For plugin authors

A plugin's view opts into the palette by setting `palette: "inline"` on its view entry
in `protoagent.plugin.yaml` (the same view that would otherwise mount in a rail/tab).
When opened from the palette, it renders the view's body in place.

> Plugin-declared *commands* (a manifest `commands:` list that contributes arbitrary
> actions, beyond views) are the next slice of ADR 0057 and not shipped yet — today a
> plugin reaches the palette via an inline **view**.

The palette is mounted in `apps/web/src/app/App.tsx` — the
`@protolabsai/ui/command-palette` substrate, opened from the keybinding intents store
(`useKbIntents().paletteOpen`) rather than a DS-internal hotkey hook: the chord is the
ordinary, rebindable `palette.toggle` binding in
`apps/web/src/keybindings/coreKeybindings.ts` ([ADR 0063](/adr/0063-keybinding-system)).
The command + view registry is built in `apps/web/src/app/usePaletteRegistry.ts`, where
core's own commands go through the same public `registerPaletteCommand` seam a fork uses
([ADR 0061](/adr/0061-frontend-extension-registries)).
