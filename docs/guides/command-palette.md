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

- **Go to any surface** — every resolvable view (Chat, Activity, Inbox, Plugins,
  Settings, plugin rail views) is a "go to" command, listed first.
- **Deep links** — jump straight to Activity/Inbox, Plugins → Discover/Install, or a
  specific Settings tab.
- **Toggle a fleet agent** — **Toggle Fleet Agent** opens a picker of the local,
  non-host fleet members with their live on/off state; picking one brings it online (or
  takes it offline) and confirms with a toast — no Settings dive. The host is never
  listed (stopping it would kill the session), and the agent this window is proxied to is
  disabled ("current") so you can't stop your own console out from under yourself.
- **Inline chat** — start typing a question and the palette morphs into a quick chat
  with the focused agent (its own thread, persisted locally) — handy for a one-off ask
  without leaving what you're doing.
- **Plugin views** — a plugin view can opt to render *inside* the palette by declaring
  `palette: "inline"` on its view (so a lightweight tool can live behind a keystroke
  instead of taking a rail slot).

Commands are ranked in a fixed order — surfaces, then deep links, then chat — so
navigation always stays at the top.

## For plugin authors

A plugin's view opts into the palette by setting `palette: "inline"` on its view entry
in `protoagent.plugin.yaml` (the same view that would otherwise mount in a rail/tab).
When opened from the palette, it renders the view's body in place.

> Plugin-declared *commands* (a manifest `commands:` list that contributes arbitrary
> actions, beyond views) are the next slice of ADR 0057 and not shipped yet — today a
> plugin reaches the palette via an inline **view**.

The palette is wired in `apps/web/src/app/App.tsx` (the `@protolabsai/ui/command-palette`
substrate + `usePaletteHotkey`); the registry is built in
`apps/web/src/app/usePaletteRegistry.ts`.
