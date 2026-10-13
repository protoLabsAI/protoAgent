# Command palette extensions and matching

For user actions and shortcuts, see [Command palette](/guides/command-palette).

## Plugin views and commands {#for-plugin-authors}

Set `palette: "inline"` on a view in `protoagent.plugin.yaml` to render its body
inside the palette. Otherwise it opens in its normal rail or tab destination.

The manifest's [`commands`](/reference/plugin-manifest#field-commands) block
contributes declarative actions: `navigate`, `open_view`, `tool`, `emit`, or
`command` (chaining to another entry). The console compiles these declarations
in `apps/web/src/app/pluginPaletteCommands.ts`; plugin code does not enter the
console bundle. The adapter checks routes and event topics against the plugin's
namespace. Unsupported or invalid actions contribute no executable row.

A `provider` declaration is parsed and shipped in status but is not compiled
into a live search source yet. A command declaring only a provider contributes
no row. Route-backed commands from enabled plugins that failed to load appear
disabled rather than offering a failing call.

## Matching and order

The empty palette uses recents and a curated root list. Each available group
gets a row before another gets a second; remaining space follows registration
order. Surfaces remain searchable even when absent from this root list.

Typed results search the whole catalog and rank matches in this order:

1. Exact label.
2. Label prefix.
3. Prefix of a word in the label.
4. Label substring.
5. Keyword, hint, group, or source match.
6. Fuzzy label match.
7. Terms spread across label and metadata without a match in one earlier tier.

Every whitespace-separated term must match somewhere. Ties use frequency,
recency, then registration order. Typed results interleave groups and omit
headers; the empty list has group headers. Picking a destination through
**Open…** also records its use.

Live sources perform their own search. Their returned rows join the ranking
without another literal query filter. Built-in knowledge search is such a
source: it requests last-token prefix matching, limits the shortlist, and adds
**All matches in Knowledge** when more results exist. Request failures show
an unavailable row; a store error answered by the API as `200` with no hits can
still appear as no matches.

## Frontend registration

`apps/web/src/app/App.tsx` and the desktop's `Launcher.tsx` mount the same
`@protolabsai/ui/command-palette` substrate. The in-app binding is the rebindable
`palette.toggle` intent in `apps/web/src/keybindings/coreKeybindings.ts`.

`apps/web/src/app/usePaletteRegistry.ts` re-exports the implementation in
`app/palette/`: `registry.ts`, `rank.ts`, `recents.ts`, and `rootView.tsx`.
Core and fork contributions use the public `registerPaletteCommand` seam
([ADR 0061](/adr/0061-frontend-extension-registries)). The console owns the root
view; read its upstream-gap notes before changing that ownership or the design
system dependency.

Settings deep links derive from `settings/sections.ts` through
`app/settingsPalette.ts`, with feature and host-console gates resolved on each
render. Developer visibility has its own channel gate and does not contribute
a generated section row. Keyboard action rows live in
`app/palette/keybindingCommands.ts`; they read current bindings rather than
hard-coding displayed chords.
