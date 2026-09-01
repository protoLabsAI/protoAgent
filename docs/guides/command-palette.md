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

- **Ask _‹agent›_** — morphs the palette into a quick chat with this window's
  agent: one preserved thread, full streaming and tool cards, `/clear` to wipe it — handy
  for a one-off ask without leaving what you're doing. (It used to be *Chat with ‹agent›*,
  which competed with the **Chat** surface for the word you were typing.)
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
  stays short. They are also **searchable from the root**: type a surface's name and it is
  there, without the hop. (It used not to be — `memory` and `knowledge` answered *No
  matches*, because those surfaces existed only inside **Open…**.)
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
  left to the keyboard — the full triage is in
  `apps/web/src/app/palette/keybindingCommands.ts`.)
- **Deep links** — the jumps worth their own command: **Settings**, **Settings: Keyboard**
  (where every chord above is rebound), **Settings: Fleet**, **Settings: Telemetry**,
  **Plugins: Discover**, **Plugins: Install from URL**.

## Two lists, not one

The palette shows a **short** list when you haven't typed anything: what you ran recently
first, then a curated root (agents, plugin views, commands). Every rail surface is
deliberately *not* in that list — there are too many of them to be useful before you've
said what you want.

Every group is guaranteed a row before any one of them takes a second, so neither a stack of
plugin views nor a full block of recents can drop a whole *section* off the list. On a first
run there's no history competing for the space and you get all of **Open…**, **Settings** and
the deep links; once your recents have taken half the list, the commands section is down to
**Open…** — one row rather than none, which is the part that matters, since **Open…** is where
browsing starts. Everything else is a keystroke away. Any slots left over are filled in
registration order.

The moment you type, the list becomes the **whole** corpus — every surface included, no
cap — ordered by how well each row matches:

1. the label IS what you typed
2. the label starts with it
3. a word in the label starts with it
4. the label contains it
5. a keyword / hint / group / source contains it
6. the label matches loosely (fuzzy)
7. it matched only by spreading your terms across the label *and* its metadata

Tier 7 is a residual, not a design goal: matching joins every field into one haystack, so a
query like `bra goals` can be admitted with `bra` in the label and `goals` in a keyword —
no single-field tier describes that, and it sorts last rather than being dropped.

Results from a plugin **source** are a separate case: a source runs its own search
(server-side, fuzzy, whatever it likes), so its rows are ordered alongside the rest but
never re-filtered — a hit whose text doesn't literally contain what you typed still shows.

The typed list has **no group headers**. A header marks where one section ends and the next
begins, which is only true while the list is in registration order; ranking sorts by match
quality *across* groups, so the sections interleave and a header would re-appear every few
rows. The untyped list is grouped, and keeps them.

Ties break on how often and how recently you've run the command, then on registration
order, so the list is stable and the thing you actually use rises — and it learns either way
you got there, whether you typed the surface's name or picked it out of **Open…**. Matching
itself is unchanged from the design system's rule — every whitespace-separated term must
appear somewhere in the row — so a row that used to be findable still is.

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
The desktop launcher window (`apps/web/src/app/Launcher.tsx`) mounts the same palette.

Both build their registry through `apps/web/src/app/usePaletteRegistry.ts`, a re-export
barrel over `apps/web/src/app/palette/` — `registry.ts` (what core contributes, through
the same public `registerPaletteCommand` seam a fork uses,
[ADR 0061](/adr/0061-frontend-extension-registries)), `rank.ts` (matching + ordering),
`recents.ts` (the frecency store) and `rootView.tsx`. That last one is the root list
itself, which the **console** owns rather than the design system; read the note at the top
of it before changing the DS dependency — it records the upstream gap, and which of the
behaviours there are fixes to the DS view that handing the root back would undo.
