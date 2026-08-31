# Command palette (⌘K)

The console has a command palette — press **⌘K** (macOS) / **Ctrl-K** (Linux/Windows)
to open it from anywhere. It's the fast path to jump between surfaces and act without
hunting through rails and menus ([ADR 0057](/adr/0057-command-palette)).

## What's in it

- **Go to any surface** — every resolvable view (Chat, Knowledge, Memory, Work, plugin
  rail views, fork surfaces) is a "go to" command. Type its name and it's there.
- **Deep links** — jump straight to Plugins → Discover/Install, or a specific Settings
  tab.
- **Fleet Room** — the co-present roster, with DM / open-console / start / stop on each
  member row. Every live member's *name* is a keyword on this command, so typing an
  agent's name lands you one step from them.
- **Inline chat** — **Ask &lt;agent&gt;** morphs the palette into a quick chat with the
  focused agent (its own thread, persisted locally) — handy for a one-off ask without
  leaving what you're doing. It sits alongside the **Chat** *surface*, which is the real
  conversation.
- **Plugin views** — a plugin view can opt to render *inside* the palette by declaring
  `palette: "inline"` on its view (so a lightweight tool can live behind the palette
  instead of taking a rail slot).

## Two lists, not one

The palette shows a **short** list when you haven't typed anything: what you ran recently
first, then a curated root (agents, plugin views, commands). Every rail surface is
deliberately *not* in that list — there are too many of them to be useful before you've
said what you want.

The moment you type, the list becomes the **whole** corpus — every surface included, no
cap — ordered by how well each row matches:

1. the label IS what you typed
2. the label starts with it
3. a word in the label starts with it
4. the label contains it
5. a keyword / hint / group / source contains it
6. the label matches loosely (fuzzy)

Ties break on how often and how recently you've run the command, then on registration
order, so the list is stable and the thing you actually use rises. Matching itself is
unchanged from the design system's rule — every whitespace-separated term must appear
somewhere in the row — so a row that used to be findable still is.

## For plugin authors

A plugin's view opts into the palette by setting `palette: "inline"` on its view entry
in `protoagent.plugin.yaml` (the same view that would otherwise mount in a rail/tab).
When opened from ⌘K, the palette renders the view's body in place.

> Plugin-declared *commands* (a manifest `commands:` list that contributes arbitrary
> actions, beyond views) are the next slice of ADR 0057 and not shipped yet — today a
> plugin reaches the palette via an inline **view**.

The palette is wired in `apps/web/src/app/App.tsx` (the `@protolabsai/ui/command-palette`
substrate) and in the desktop launcher window (`apps/web/src/app/Launcher.tsx`); both
build their registry through `apps/web/src/app/usePaletteRegistry.ts`, a re-export barrel
over `apps/web/src/app/palette/` — `registry.ts` (what core contributes), `rank.ts`
(matching + ordering), `recents.ts` (the frecency store) and `rootView.tsx` (the root list
itself, which the console owns rather than the design system: see the note at the top of
that file for the upstream gap and when it can be retired).
