# Command palette (⌘K)

The console has a command palette — press **⌘K** (macOS) / **Ctrl-K** (Linux/Windows)
to open it from anywhere. It's the fast path to jump between surfaces and act without
hunting through rails and menus ([ADR 0057](/adr/0057-command-palette)).

## What's in it

- **Go to any surface** — every resolvable view (Chat, Activity, Inbox, Plugins,
  Settings, plugin rail views) is a "go to" command, listed first.
- **Deep links** — jump straight to Activity/Inbox, Plugins → Discover/Install, or a
  specific Settings tab.
- **Inline chat** — start typing a question and the palette morphs into a quick chat
  with the focused agent (its own thread, persisted locally) — handy for a one-off ask
  without leaving what you're doing.
- **Plugin views** — a plugin view can opt to render *inside* the palette by declaring
  `palette: "inline"` on its view (so a lightweight tool can live behind ⌘K instead of
  taking a rail slot).

Commands are ranked in a fixed order — surfaces, then deep links, then chat — so
navigation always stays at the top.

## For plugin authors

A plugin's view opts into the palette by setting `palette: "inline"` on its view entry
in `protoagent.plugin.yaml` (the same view that would otherwise mount in a rail/tab).
When opened from ⌘K, the palette renders the view's body in place.

> Plugin-declared *commands* (a manifest `commands:` list that contributes arbitrary
> actions, beyond views) are the next slice of ADR 0057 and not shipped yet — today a
> plugin reaches the palette via an inline **view**.

The palette is wired in `apps/web/src/app/App.tsx` (the `@protolabsai/ui/command-palette`
substrate + `usePaletteHotkey`); the registry is built in
`apps/web/src/app/usePaletteRegistry.ts`.
