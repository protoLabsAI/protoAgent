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
- **Keyboard actions** — the console's own shortcuts, as ordinary rows you can run by
  name: **New chat**, **Clear conversation**, **Focus chat composer**, **Next chat tab** /
  **Previous chat tab**, **Toggle latest tool block**, and the **left rail** / **right
  panel** / **bottom dock** toggles. Each row shows the chord it is bound to *right now* —
  rebind one in Settings ▸ Keyboard and the row re-labels itself — so the palette doubles as
  the shortcut cheat-sheet: type `shortcuts` to list the whole set. A chat action navigates
  to chat before it runs — re-opening the dock chat lives on if you had it collapsed — so
  picking one from Knowledge, Settings, or a folded-away rail does what you meant.
  (Not every binding gets a row. A shortcut whose row would open the thing you are already
  looking at, duplicate another row's action, or land somewhere it can't act is deliberately
  left to the keyboard — the full triage is in `apps/web/src/app/keybindingCommands.ts`.)
- **Deep links** — the jumps worth their own command: **Settings**, **Settings: Keyboard**
  (where every chord above is rebound), **Settings: Fleet**, **Settings: Telemetry**,
  **Plugins: Discover**, **Plugins: Install from URL**.

Groups render in registration order — **Agents**, then **Plugins**, then **Commands** —
so the agent and its fleet stay at the top.

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
