# ADR 0057 — Command palette (⌘⇧K): plugin-extensible quick command

- **Status:** Accepted (proposed 2026-06-18; shipped v0.48.0)
- **Date:** 2026-06-18
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** ux, command-palette, plugins, desktop, navigation, extensibility, ds
- **Related:** consumes the DS `@protolabsai/ui/command-palette` (protoContent
  [ADR 0006](https://github.com/protoLabsAI/protoContent/blob/main/docs/decisions/0006-command-palette-extensible-substrate.md),
  shipped in protoContent #266). Reuses [ADR 0026](./0026-plugin-contributed-console-surfaces.md)
  (plugin iframe views), [ADR 0044](./0044-plugin-driven-console-navigation.md)
  (plugin nav surfaces), [ADR 0045](./0045-chat-panel-slot.md) (chat slot),
  [ADR 0039](./0039-plugin-event-bus.md) (event bus / plugin dots),
  [ADR 0042](./0042-fleet-supervisor-unified-console.md) (slug proxy + bearer),
  [ADR 0038](./0038-generative-ui-artifacts-two-mode.md) (two-mode iframe plugin UI / theme handshake),
  [ADR 0056](./0056-unified-dockable-view-model.md) (the unified `View` /
  `viewFor(id)` it forces). Touches `apps/web` (`app/App.tsx` surface registry,
  `state/uiStore.ts`, `app/PluginView.tsx`), `graph/plugins/manifest.py`, and
  `apps/desktop` (Tauri global shortcut).

> The DS already ships the hard part: a morphing command palette built on a reactive
> contribution **registry**, async command **providers** (live search), an iframe
> **`pluginView()`** that runs the `protoagent:init`/`theme` handshake (the *sender*
> half; `plugin-kit.js` is the receiver), and three **presentation** modes
> (overlay / inline / fullscreen). This ADR is the protoAgent wiring: a thin web
> **adapter** that feeds the registry from the existing surface registry + each
> enabled plugin's manifest, a **new declarative `commands:` manifest contribution
> type**, and a **palette trigger** (web hotkey + Tauri global shortcut). The crux: every
> existing surface already opens via `useUI().setSurface(id)`, so it
> **auto-becomes a palette command** — plugins get palette presence for free, and
> `commands:` only adds the actions *beyond* navigation.

## 1. Context & problem

There is **no command palette / quick-command in the console today** (greenfield — no `cmdk`
dependency, no global hotkey, only a `Palette` lucide icon in the theme button).

The console already has everything a palette needs to *navigate*:

- **One navigation entry point.** `useUI().setSurface(id)` (`state/uiStore.ts`) opens
  any view — core (`CORE_SURFACES`, `app/App.tsx:494`), plugin
  (`plugin:<id>:<view>`, derived at `App.tsx:300-312`), or fork/ext
  (`src/ext/registry.ts`). Sub-surfaces add a second setter (`setActivityTab`,
  `setSettingsScope`/`setSettingsSection`, `setBoxTab`, …).
- **Plugins already contribute iframe views** (ADR 0026/0044): the backend
  surfaces `runtime.plugins[].views` (`graph/plugins/manifest.py`), the web
  reconciles them into `railOrder` (`App.tsx`, `reconcilePluginViews`), and
  `app/PluginView.tsx` renders each as a sandboxed iframe with the
  `protoagent:init` handshake — theme from `consoleTheme()` (the 6-key set read
  from CSS vars `--bg`/`--bg-panel`/`--fg`/`--fg-muted`/`--brand-violet-light`/`--border`,
  `PluginView.tsx:18-26`) and bearer from `authToken()`
  (`localStorage["protoagent.authToken"]`, `PluginView.tsx:74`).

The DS shipped a **plugin-extensible palette substrate** (protoContent ADR 0006):
registry, providers, `pluginView()`, presentation modes — host-agnostic, no
protoAgent knowledge. The problem this ADR solves: **how do sandboxed plugins
(manifest + Python `register()` + iframe pages) contribute palette commands and
views, and how does the same component serve the in-app palette *and* a desktop
quick-command — without editing core per plugin and without importing plugin code.**

## 2. Decision

Consume the DS substrate behind a thin protoAgent adapter. Five parts:

**A. One app registry, fed from existing sources.** A web module owns a
`createPaletteRegistry()` and registers three command sources:

1. **Navigation commands, auto-derived from the surface registry.** Every
   `CORE_SURFACE` + ext surface + enabled plugin view becomes a *"Go to X"*
   command whose `run` is `useUI().setSurface(id)` (plus sub-tab setters for
   deep-links like *Settings ▸ Workspace ▸ Memory*). **No manifest needed** — a
   plugin that contributes a view already earns a palette command. Built on
   `viewFor(id)` (§4).
2. **Plugin-declared commands** from each enabled plugin's manifest `commands:`
   (§3) — the actions *beyond* navigation.
3. **Core app commands** — new chat session, toggle theme, open a settings
   section, run a `user_facing` skill / slash-command (ADR 0052), etc.

**B. New manifest `commands:` contribution type** (declarative data, exactly like
`views:`). Parsed in `graph/plugins/manifest.py` (mirror `_parse_views` →
`_parse_commands`), surfaced on runtime status as `plugins[].commands` (like
`plugins[].views`), consumed by the adapter. Each command's declarative `action`
is **compiled to a `run(ctx)` by the trusted adapter** — the web is the single
dispatch authority; **plugin code never enters the bundle**.

**C. Plugin views in the palette, two modes:**

- **Navigate** (default): `action: { type: navigate, view: <id> }` →
  `setSurface("plugin:<id>:<view>")` opens the view in its rail/panel via the
  existing `PluginView.tsx`. Free for any existing view.
- **Inline morph** (opt-in): `action: { type: open_view, view: <id>, inline: true }`
  → `ctx.enter(pluginView({ url, theme, token }))` morphs the palette **body**
  into the plugin's iframe (transient, in-palette), passing `consoleTheme()` +
  `authToken()` so the page themes/authenticates identically to a rail view.

**D. Live search providers.** A `provider:` manifest entry compiles to a DS
`CommandProvider.getCommands(query)` that calls the plugin's search route
(`apiFetch("/api/plugins/<id>/<route>?q=…")`, bearer + slug-aware) and maps result
rows to commands. Debounced + cancellable (DS-side).

**E. Palette trigger, two surfaces, one component:**

- **In-app:** `<CommandPalette presentation="overlay">` mounted over the AppShell in
  `App.tsx`, toggled at ⌘⇧K. *(This proposed the DS's own `usePaletteHotkey()` on ⌘K, and
  neither half shipped: ⌘K became chat **Clear conversation** in #2949, and the trigger is
  an ordinary rebindable keybinding — `palette.toggle` / `mod+shift+k` in
  `apps/web/src/keybindings/coreKeybindings.ts`, ADR 0063 — driving the open-state the
  palette reads from the intents store, not a DS-internal hook. `usePaletteHotkey` has no
  callers in `apps/web/src`.)*
- **Desktop:** a Tauri global shortcut → show the window + emit a Tauri event the web
  listens for to open the palette. **v1 reuses the single `main` window** (overlay); a
  dedicated frameless `presentation="fullscreen"` palette window is **v2**.
  *(This bullet originally argued the shortcut was free to take because ⌘K was unclaimed.
  That premise no longer holds — ⌘K is chat **Clear conversation** in-app — and the shipped
  desktop chords are neither ⌘K nor ⌘⇧K anyway: the quick launcher is **⌥Space** on macOS /
  **Ctrl+Alt+Space** elsewhere, and **⌘⇧P** toggles the console window. See
  `default_hotkeys()` in `apps/desktop/src-tauri/src/lib.rs`, operator-overridable from
  Settings ▸ Keyboard.)*

## 3. The `commands:` manifest (new)

Declarative YAML, never imported — same trust posture as `views:`:

```yaml
commands:
  - id: search                       # adapter namespaces → plugin:<id>:search
    title: Search files
    hint: by name
    keywords: [file, find, open]
    icon: Search                     # lucide name, like views[].icon
    group: Files
    action: { type: open_view, view: browser, inline: true }
  - id: reindex
    title: Reindex workspace
    action: { type: tool, route: reindex, method: POST }   # /api/plugins/<id>/reindex
  - id: files-search                 # live results, not a fixed command
    title: Files
    provider:
      route: search                  # GET /api/plugins/<id>/search?q=…
      result_action: { type: open_view, view: browser, inline: true }

views:
  - { id: browser, label: Files, icon: Folder, path: /plugins/files/browser,
      palette: inline }              # opt this view into the palette's INLINE morph
```

> **As shipped (#3282).** Auto-nav went default-on (§8), so a view is a palette entry
> without opting in and `views[].palette` is *only* the inline-morph switch: the honored
> spellings are the literal `inline` and a `{ path: … }` mapping naming a different page
> to morph. `palette: true` warns and is dropped. The parser ships the whole §4 action
> set, normalizes `open_view` to `inline: true` (there is no non-inline `open_view` —
> `navigate` is that), and requires a `provider`'s `result_action`.

Backend: add `_parse_commands` next to `_parse_views` (`graph/plugins/manifest.py:325`), store on
`PluginManifest.commands`, and expose it on the runtime status the way views are
(`installer.py:287` / status payload). The web reads `plugins[].commands` beside
`plugins[].views` (`apps/web/src/lib/types.ts`).

## 4. Adapter & the `viewFor(id)` façade

- **Implement `viewFor(id) → View`** — ADR 0056's missing façade (it is *Proposed*;
  resolution is three separate paths today: `coreMeta` `App.tsx:514`,
  `allPluginViews` `App.tsx:300-312`, `registeredSurfaces()` `src/ext/registry.ts`).
  The palette's nav-command source *is* this façade, so building a minimal
  `viewFor(id)` here (`{ id, kind, title, icon }`) **advances ADR 0056's open item
  instead of duplicating it**.
- **Adapter sketch** (`apps/web`, e.g. `state/paletteRegistry.ts` + a
  `usePaletteRegistry()` hook):

  ```ts
  function usePaletteRegistry() {
    const registry = useMemo(() => createPaletteRegistry(), []);
    const ui = useUI();
    const runtime = useRuntimeStatus();              // existing query
    const theme = consoleTheme(); const token = authToken();  // PluginView.tsx helpers

    // 1. nav commands from every resolvable view (built on viewFor)
    useEffect(() => registry.registerCommands(
      navViews().map(v => ({ id: `nav:${v.id}`, label: `Go to ${v.title}`,
        icon: v.icon, group: v.kind, run: () => ui.setSurface(v.id) })),
      { source: CORE }), [/* views */]);

    // 2. plugin commands + inline views, registered on enable / torn down on disable
    useEffect(() => {
      const offs = enabledPlugins(runtime).flatMap(p => {
        const src = { id: `plugin:${p.id}`, label: p.name };
        const cmds = (p.commands ?? []).map(c => compile(c, p, ui, theme, token));
        const inline = (p.commands ?? []).filter(isInlineView)
          .map(c => pluginView({ id: `plugin:${p.id}:${c.action.view}`,
            url: pageUrl(p, c.action.view), theme, token, source: src }));
        return [registry.registerCommands(cmds, { source: src }),
                registry.registerViews(inline)];
      });
      return () => offs.forEach(off => off());
    }, [runtime, theme, token]);

    return registry;
  }
  ```

- **Action dispatch** — the *only* place plugin data becomes behavior, all in the
  trusted adapter:

  | `action.type` | → `run(ctx)` |
  |---|---|
  | `navigate`   | `ui.setSurface(view)` (+ sub-tab setters for deep-links) |
  | `open_view` (`inline`) | `ctx.enter("plugin:<id>:<view>")` (a registered `pluginView`) |
  | `tool`       | `apiFetch("/api/plugins/<id>/<route>", { method })` → toast → `ctx.close()` |
  | `emit`       | `POST /api/events/publish` (ADR 0039, the bus `PluginView` already relays) |
  | `command`    | look up + run another command |

> **As shipped (#3294).** The adapter is `apps/web/src/app/pluginPaletteCommands.ts`, wired by
> `usePaletteRegistry` for BOTH the console window and the frameless desktop launcher off one
> shared derivation (`pluginCommandSources`) rather than the two hand-synced copies the
> plugin-*view* derivation still carries. Rows register per plugin, adjacent to that plugin's
> view rows, stamped with `{source}` so the palette root renders the attribution chip and its
> contiguous-group rendering keeps a single "Plugins" heading — a manifest `group` naming a
> heading the console already owns ("Agents", "Commands") falls back to that plugin section
> rather than opening a second one mid-list. Five deltas from the sketch above:
>
> - **Navigation goes through the serializable `NavIntent` chokepoint**, not `ui.setSurface`.
>   The launcher mounts this same registry in a shell-less JS context where store mutations are
>   inert, so a direct store call is a silent no-op there.
> - **The adapter re-validates every route and topic namespace** instead of trusting
>   `/api/runtime/status`. `apiUrl()` forwards an absolute URL unchanged and the operator bearer
>   is attached, so an escaped route is an authenticated write, not a blank iframe — and the
>   payload it reads never has to have passed through `_parse_commands` (a stale cache, a parser
>   regression, a hand-edited response). Failing the mirror produces no row.
> - **`open_view` falls back to `navigate`** when the target view never opted into the inline
>   morph through `views[].palette` — something `_parse_commands` cannot see, and `ctx.enter` on
>   an unregistered view id blanks the whole palette.
> - **A view action may only name a NAVIGABLE view.** Declared is not navigable: a `slot: "chat"`
>   claimant renders under the core chat id and a `utility` widget is a bottom-left pill, so
>   neither joins `railOrder` and neither has a `plugin:<id>:<view>` surface. The allow-set is
>   built through the one predicate all three hosts share (`lib/pluginViews.ts`
>   `isNavigablePluginView`), so a command at one of those produces no row — rather than a live
>   "go to" that sets a surface nothing renders, which App's stale-surface fallback answers by
>   dropping the operator on chat. `_parse_commands` mirrors it, because only the parse side can
>   warn the plugin author.
> - **A launcher row's `tool`/`emit` OUTCOME is forwarded to the console window.** Firing one
>   closes the palette, and on the launcher closing the palette hides the window — a toast
>   raised there renders into a webview nobody can see. It rides `palette:notify` to the main
>   window, which is raised the same way a `navigate` row raises it.
> - **A `tool` row on an enabled plugin that failed to LOAD ships disabled**, with the reason
>   where its hint goes. That route is served by the plugin's own router, so there is nothing to
>   call; `emit` (the core bus route) and the view actions (the view host shows the loader's real
>   error) stay live. Disabling rather than hiding follows the Fleet Room command's convention —
>   a row that explains itself is discoverable, a row that vanishes reads as never shipped.
> - **A row with no manifest `hint` says what it DOES** — "go to" for a `navigate`, "run" for a
>   `tool`/`emit` — and carries that word in its keywords either way. `rank.ts` matches on
>   `label + hint + group + source.label + keywords`, and the plugin's own view rows already hint
>   "go to", so without it a plugin-declared navigation row is the one navigation row in the
>   palette that typing "go to" misses. "open" is deliberately not used: `Open…` owns it.
>
> `provider` rows are **not** compiled: the §8 provider budget (per-query timeout/cancel,
> per-plugin result cap) is still open, so a provider-only entry contributes no row.

## 5. Sequencing

1. **Bump `@protolabsai/ui`** to the release carrying `/command-palette`
   (≥ the protoContent #266 publish; currently on `^0.43.0`).
2. **`viewFor(id)` + nav-command auto-derivation + core commands + palette overlay**
   (no plugins yet). Ships a **useful palette immediately** — quick-jump to every
   surface + core actions.
3. **Backend `_parse_commands` + runtime `commands` + the adapter's plugin
   command / provider / inline-view wiring** + manifest doc + `plugin-devkit`
   `building-plugins` update.
4. **Desktop palette** — Tauri global shortcut + window-open event.

Step 2 ships value alone; 3 adds plugin extensibility; 4 adds desktop. Each is
independently shippable.

## 6. Alternatives considered

- **Bespoke palette in the web app** — rejected; the DS substrate exists, is
  theme-aware, and is reused across consumers (desktop / in-app / future cockpit).
- **Plugins ship React command modules** — rejected; plugins are sandboxed and
  out-of-bundle (the entire reason for declarative commands + iframe views).
- **Imperative `register_palette_command()` in Python `register()`** — rejected for
  v1; commands are UI contribution data like `views:`, so a declarative manifest is
  consistent and keeps the web as the single dispatch authority. Revisit if a plugin
  needs *dynamically computed* commands the manifest can't express.
- **Navigation-only (no `commands:`)** — the legitimate **stopping point after step
  2** if the plugin-command surface isn't worth the backend + devkit cost. The
  auto-derived nav palette must justify the rest against this baseline.

## 7. Consequences

- **Pro:** the palette across every surface immediately (step 2); plugins extend it with
  **zero core edits** (register on enable, unregister on disable — mirrors
  `reconcilePluginViews`); one DS component serves in-app + desktop; **advances ADR
  0056** by forcing `viewFor(id)`.
- **Con:** a new manifest contribution type (backend parse + runtime status +
  devkit/docs); a small trust surface (declarative `action`s — keep the set minimal,
  dispatch only in the adapter); desktop shortcut is Rust work.
- **Trust:** inherits *install ≠ enable ≠ trust* — palette entries appear only for
  **enabled** plugins; no plugin code in the bundle; iframe sandbox + bearer +
  slug-aware exactly as console views today.

## 8. Open questions

- **Action set v1** — `navigate` / `open_view(inline)` / `tool` / `emit` / `command`
  / deep-link: which ship first? (rec: `navigate` + `tool` + `open_view`.)
  **Settled (#3282):** all five. `command` is in *because* it is a namespace boundary
  like the others — left unvalidated, a manifest could name a core command id the
  adapter registered — so it is confined to the plugin's own declared commands, with the
  chain resolved to a fixed point so a loop or a dangling hop drops.
- **Auto-nav for every view, or opt-in** via `palette: true` / `surfaces:[palette]`?
  (rec: default-on, let noisy views opt *out*.)
  **Settled:** default-on; `palette` means the inline morph only (see §3).
- **Provider budget** — per-query timeout/cancel + per-plugin result caps so a slow
  plugin can't stall the palette.
- **Desktop** — reuse the `main` window overlay (v1) vs a dedicated frameless
  palette window (v2); global-shortcut conflict policy alongside ⌘⇧P.
- **`when` context predicates** (gate a command by app state) — defer to v2; the DS
  doesn't need them, the adapter can filter.
- **Inline `pluginView` + the event-bus relay** (`protoagent:publish`, which
  `PluginView.tsx` wires) — does an inline view need it, or is `navigate` the right
  answer for rich interactive plugin views? (rec: inline = read / quick-action;
  rich interaction → `navigate` to the real surface.)
