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
- **Chat** — the chat's own slash commands, the ones that used to exist only inside the
  composer's `/` menu: `/clear`, `/export`, `/model`, `/compact`, `/incognito`, `/perf` and
  the rest, each listed as `/token · what it does` and searchable by the words you'd
  actually reach for ("wipe" finds `/clear`, "llm" finds `/model`). A command that acts on
  the chat in front of you stays listed with no chat open but is visibly disabled and says
  why; one that just needs somewhere to work opens or focuses a chat first. They stay listed
  when the chat panel is hidden, too — running one brings the panel back first.
  The two per-tab **modes**, `/bypass` and `/incognito`, are the exception to "picking a row
  runs it": their row shows the current setting (`… — now off`) and then hands you the
  composer with `/bypass ` typed, so you say which way and press Enter yourself. Nothing in
  the palette can turn off tool-approval prompts on its own.
- **Skills** — every *user-facing* skill you or a plugin has installed. A skill isn't
  something the console runs: the server folds its procedure into your **next message**, so
  picking one takes you to the chat with `/‹skill› ` typed and leaves the send to you (the
  row says so). The token lands in *front* of anything already in the composer rather than
  replacing it, so you can reach for a skill mid-message. `/btw` behaves the same way, since
  it needs the question you were going to ask.
- **Plugin views** — each enabled plugin's views are their own group. A view can also opt
  to render *inside* the palette by declaring `palette: "inline"` on it (so a lightweight
  tool can live behind a keystroke instead of taking a rail slot).
- **Plugin commands** — a plugin can also declare rows that aren't views at all, in its
  manifest's `commands:` block: go to one of its views, run one of its routes, or publish
  an event. They sit with that plugin's view rows and carry its name as a chip, so it is
  clear which plugin a row belongs to. A plugin that is enabled but failed to load shows
  its route-backed rows greyed out saying so, rather than offering a call that could only
  fail.
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
- **Deep links** — **Settings** (opens the dialog wherever you left it, and shows its **⌘,**
  chord on the row — read live from the binding, so it follows a rebind), **Settings:
  Keyboard** (where every chord above is rebound), **Plugins: Discover**, **Plugins: Install
  from URL**, and a **Settings: `<Section>`** row for *every*
  section of the Settings dialog — Theme, Keyboard, Model, Tools, MCP, Skills, Subagents,
  Delegates, Snapshot and the rest. Those rows are GENERATED from the section table
  (`apps/web/src/settings/sections.ts`) by `apps/web/src/app/settingsPalette.ts`, so a new
  section is deep-linkable the moment it is declared rather than when somebody remembers to
  add a command. Each row wears its Settings-rail glyph and a trailing hint naming its nav
  heading (Agent · Capabilities · Box · This console) — the hint is searchable too, so typing
  `capabilities` lists exactly those five.
  Search by what you'd *say*, not by the label: `shortcuts` finds Keyboard, `dark mode`
  Theme, `api key` Model, `rag` Knowledge, `a2a` Delegates, `backup` Snapshot, and `port` or
  `network` the box-runtime knobs that live behind a chip on Fleet. (Keywords are synonyms
  only — the matcher already searches each row's label, hint and group.)
  The per-section rows are registered after the three above, so the **root** list is
  unchanged — they earn their place on search, and through recency once you've used one.
  A section behind a developer flag (Secrets, Devices, Publish) or restricted to the host
  console (Overview, Telemetry) carries that gate on the row and is resolved *per render*,
  never at registration. The one section with no row is **Developer**: its visibility is a
  channel decision (`developerPanelVisible`), which is neither of the two axes the seam can
  gate on — see the comment in `settingsPalette.ts`.
- **Knowledge** — type two or more characters and the palette searches the agent's
  knowledge store live ([ADR 0021](/adr/0021-agent-memory-architecture)) — findings,
  notes, the daily log, harvested sessions — and lists the top matches among your results,
  each tagged with a **Knowledge** chip. Each row is trailed by where that entry came from
  (its source file, or failing that its domain).
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
- **Chats** — every open chat tab, by title, so you land on the conversation about the
  release notes by typing "release" instead of counting tab positions. Type the name and press
  Enter; there is nothing to wait for, and the rows narrow with every keystroke like the rest
  of the list. The list follows the tab strip as it moves: a chat you close drops out, a new
  one appears, and a chat renames itself once its first message gives it a title. The one
  you're on is marked *current* (and still runs — that's how you get back to chat from
  Knowledge or Studio), and the first nine carry their `⌘1`–`⌘9` jump shortcut, rendered live
  so it keeps up with a rebind. Typing what a chat *is* finds them too — "switch", "jump",
  "tabs", "sessions", "threads", or "incognito" for the memory-free ones.

## Two lists, not one

The palette shows a **short** list when you haven't typed anything: what you ran recently
first, then a curated root (agents, plugin views, commands, chats). Every rail surface is
deliberately *not* in that list — there are too many of them to be useful before you've
said what you want.

Every group is guaranteed a row before any one of them takes a second, so neither a stack of
plugin views, a wall of open chats, nor a full block of recents can drop a whole *section* off
the list. On a first run there's no history competing for the space and you get all of
**Open…**, **Settings** and the deep links; once your recents have taken half the list, the
commands section is down to **Open…** — one row rather than none, which is the part that
matters, since **Open…** is where browsing starts. Everything else is a keystroke away. Any
slots left over are filled in registration order.

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

Live **Knowledge** results are a source in exactly that sense — the first one the console
ships itself. They are fetched per keystroke rather than registered up front, so they land a
moment after the rest of the list and are ordered into it rather than appended below it: a
chunk whose heading is what you typed sorts above a command that merely mentions it. Because
the typed list has no headers, each one carries a **Knowledge** chip instead, which is what
marks it as an entry from the store rather than another console command.

## For plugin authors

A plugin's view opts into the palette by setting `palette: "inline"` on its view entry
in `protoagent.plugin.yaml` (the same view that would otherwise mount in a rail/tab).
When opened from the palette, it renders the view's body in place.

Beyond views, a plugin contributes rows through its manifest's `commands:` block — see
[`commands`](/reference/plugin-manifest#field-commands) for the field itself. Each entry
declares what it *does* (`navigate`, `open_view`, `tool`, `emit`, or `command`, chaining to
another entry) and the console compiles that declaration into the row's behaviour inside its
own trusted adapter, `apps/web/src/app/pluginPaletteCommands.ts`. That indirection is the
point: plugin code never enters the console bundle, so the manifest is data and this adapter
is the single place data becomes behaviour. It re-checks every route and event topic against
the plugin's own namespace instead of trusting the status payload, and anything that fails —
along with any action it does not implement — contributes no row at all rather than a row
that fires something its author did not write.

> A `provider` entry (a live search that queries the plugin as you type) is parsed and
> shipped on the status payload but not compiled yet, so an entry declaring only a provider
> still contributes nothing.

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
