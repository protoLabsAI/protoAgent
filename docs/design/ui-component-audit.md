# Console UI audit — surfaces, panels, and the upstream `@protolabsai/ui` wishlist

**Date:** 2026-06-08 · **Scope:** `apps/web` (the operator console) · **DS baseline:** `@protolabsai/design@0.4.0` + `@protolabsai/ui@0.4.0`

This is the inventory we hand to the protoLabs UI team. Three parts: **(1)** the surface/panel map (our IA), **(2)** the component inventory + DS-adoption status, **(3)** the gap analysis → prioritized upstream requests.

Context: ADR 0037 put the DS foundation in (Tailwind + `@protolabsai/design` preset + `--pl-*` tokens, shadcn/Radix). Federation was retired (ADR 0038) — plugin UI is now sandboxed iframe + the `src/ext` build-time seam. The **cross-board refinement sweep (task #68) is still deferred**; this audit scopes it and separates "swap to an existing DS component" from "needs a new DS component."

---

## 1. Surface & panel inventory (the IA)

The console is a **dual-rail shell** (ADR 0035): an icon rail → swappable surfaces on a left + right rail, unified tabs, resize handle, mobile shell <768px. Rendered through one `renderSurface(id)` switch in `app/App.tsx`.

### Top-level rail surfaces

| Surface | Sub-tabs (StageSubnav) | Component(s) | Notes |
|---|---|---|---|
| **Chat** | — | `chat/ChatSurface` (+ `ToolCalls`, `tool-renderers`, `HitlForm`, `Markdown`) | Streaming; mounts on whichever rail holds it (#613). Highest-traffic surface. |
| **Activity** | Thread · Inbox | `activity/ActivitySurface`, `inbox/InboxPanel` | Inbox tab carries an unread badge. |
| **Studio** | — | `workflows/WorkflowsSurface` (+ `WorkflowBuilder`) | Workflows-only since ADR 0020. |
| **Knowledge** | Store · Settings | `knowledge/KnowledgeStore`, `settings` (Memory category) | |
| **Agent** | Identity · Settings · Tools · MCP · Subagents · Skills · Middleware | `IdentityPanel`, `SettingsCategory`, `ToolsPanel`, `McpPanel`, `SubagentsPanel`, `PlaybooksSurface`, `MiddlewarePanel` | 7-tab hub — the densest surface. |
| **Plugins** | Local · Market · Download | `plugins/PluginsSurface`, `settings/PluginsSection` | |
| **Settings** | (SETTINGS_TABS categories) | `settings/SettingsSurface`, `SettingsCategory`, `OverviewPanel`, `DelegatesSection` | Form-heavy. |
| **Tasks** | — | `app/TasksPanel` | Right-rail panel by default. |
| **Goals** | — | `app/GoalsPanel` | |
| **Schedule** | — | `schedule/SchedulePanel` | |

### Non-rail / lifecycle surfaces

| Surface | Component | Notes |
|---|---|---|
| **Setup wizard** | `setup/SetupWizard` (716 lines) | Pre-boot via `app/BootGate`; full form/stepper flow. |
| **Intro splash** | `app/IntroSplash` | Once per session (#766). |
| **Plugin views** | `app/PluginView` | Dynamic `plugin:<id>:<view>` — sandboxed iframe (ADR 0038). |
| **Fork surfaces** | `ext/registry` (`src/ext` seam) | Build-time, in-process, trusted forks (ADR 0038 D3). |
| **Error boundary** | `app/ErrorBoundary` | |

### Shell chrome (the AppShell candidates)

`components/SurfaceRail` (icon rail + tabs), `components/MobileNav` (bottom quick-bar + hamburger drawer), `app/PanelHeader` (canonical header, 59 uses), `app/StageSubnav` (icon+badge tab strip, 21 uses), `app/StatusPill`, `app/ScrollArea`, `app/ConfirmDialog`, `contextMenu/*` (registry + renderer on local Radix dropdown).

---

## 2. Component inventory & DS-adoption status

`@protolabsai/ui@0.4.0` **actually ships** (verified against installed source): Button, Badge, Card, Tabs, Field, Dialog, ConfirmDialog, Drawer, ToastProvider/useToast, Tooltip, Table (+THead/TBody/Tr/Th/Td), StatusDot, Spinner, ScrollArea, Input, **Textarea**, Select, Switch, Checkbox — plus marketing/layout primitives (Hero, Stat/Stats, Steps, Callout, Kbd, Empty, Divider, TextLink, Board…).

**Adopted so far:** `Button` (1 call site), `ToastProvider` + `useToast` (2). Everything else is still bespoke.

### Migration surface — local pattern → existing DS component (the #68 sweep)

| Local pattern | Count | → DS component | Effort |
|---|---:|---|---|
| raw `<button>` | 93 | `Button` (variants) | High volume, mechanical |
| raw `<input>` | 57 | `Input` | High volume |
| `<select>` | 12 | `Select` | Low |
| `<textarea>` | 12 | `Textarea` | Low |
| `type=checkbox` | 6 | `Checkbox` / `Switch` | Low |
| `StatusPill` | 29 | `Badge` (labeled) / `StatusDot` (bare) | Medium |
| local `ScrollArea` | 11 | `ScrollArea` | Low — drop-in |
| local `ConfirmDialog` | 10 | `ConfirmDialog` | Low — drop-in |
| `.badge` / count chips | 45 | `Badge` | Medium |
| `<table>` | 2 | `Table` family | Low |
| spinner/loading | 9 | `Spinner` | Low |
| native `title=` tooltips | 89 | `Tooltip` | Medium (a11y win) |
| `useToast`/toast | 2 | `Toast` ✅ already | — |

**These need no new DS work** — they're the board-by-board swap, each a HELD draft PR per the local-test gate.

---

## 3. Gap analysis → upstream requests for the protoLabs UI team

What the console needs that **`@protolabsai/ui@0.4.0` does not provide.** Ordered by priority.

### P0 — blocks the foundation

1. **`Menu` / `DropdownMenu`** (Radix-backed, imperative open-at-coords). The single true primitive gap. Our ADR 0036 context-menu system keeps a local `components/ui/dropdown-menu.tsx` solely because the DS has no menu. Needs: right-click/imperative trigger, nested items, separators, keyboard a11y, open-at-`{x,y}`. **We have a proven implementation to hand over.**

2. **`AppShell` / dual-rail layout.** protoContent deferred AppShell ("spec against both dashboards") — **our ADR 0035 shell *is* that spec**, proven in production: icon rail → swappable left/right rails (`railOrder`), resize handle, unified tabs, mobile shell (<768px bottom-bar + drawer), persisted UI state. Sub-pieces to lift: `SurfaceRail` (icon rail + tab strip), `MobileNav`. **Converge upstream rather than diverge** — this is the headline coordination item.

### P1 — high-traffic composites worth standardizing

3. **`PanelHeader`** — our most-used composite (59 call sites): title + optional kicker + right-aligned actions, with a `compact` variant for nested panels. Not covered by the marketing-oriented `Hero`/`Heading`. Clean, props-driven, token-only — ideal extraction candidate.

4. **`Tabs` — add `icon` + `badge` slots.** DS `TabItem` is `{id, label, disabled, locked}`. Our `StageSubnav` (21 uses) needs a leading **icon** and a trailing **badge** (e.g. Inbox unread count). Small additive change to the existing `TabItem` type; would let us retire `StageSubnav`.

### P2 — one real gap; the rest verified as already-covered

5. **`ScrollArea` parity** ([#134](https://github.com/protoLabsAI/protoContent/issues/134)) — adopting DS `ScrollArea` showed `.pl-scroll` drops `min-height:0` (breaks flex/grid-child scroll), `overscroll-behavior`, and any focus ring for a focusable region. We bridge it app-side today (PR #767); filed to fold upstream.

**Verified against installed 0.4.0 source — these are NOT gaps, just adopt:**
- **Banner/Alert** → DS **`Callout`** `{tone, title, children}` covers settings-banner + `panel-error`.
- **Metric** → DS **`Stat`/`Stats`** covers telemetry `.metric`.
- **StatusPill** → DS **`StatusDot`** has a `label` prop (and `Badge` for the pill form) — clean map.
- **Form fields** → DS **`Field`** has `multiline`/`readOnly`/`onValueChange`.
- **EmptyState** → DS **`Empty`** is a bare styled wrapper; compose icon/title/action inside it (workable, not filed).
- **Markdown** → stays app-side (chat-specific affordances); DS `Prose` only for static prose.

### Not for the DS (stays app-local)

Context-menu **registry/store** (`ContextType` keying is app domain logic — only the *renderer* needs the DS Menu), chat tool-renderers, the workflow builder canvas, plugin-iframe host, setup-wizard flow logic.

Settings IA (ADR 0048, #916) — all compose DS primitives (`Dialog`, `Button`, `Badge`, `Tabs`, `PanelHeader`, `Input/Select/Textarea`, `ThemePanel`); the logic is app-coupled, so they stay local:
- **`QuickSetting`** — a gear→dialog that edits named fields via *our* settings schema + the `/api/settings` cascade (host-scoped → host layer). The *pattern* is reusable, but the binding is app domain.
- **`SettingsOverlay`** — wraps our two-home `SettingsSurface` in a DS `Dialog`.
- **`ThemeQuickButton`** — wraps our per-agent `ThemeSurface` (which calls `/api/theme`) in a DS `Dialog`.
- **`CommonsPanel`** — the box-shared skills commons (ADR 0041).

---

## Filed upstream (protoContent)

**Coordination thread:** [protoContent #137](https://github.com/protoLabsAI/protoContent/issues/137) — the umbrella discussion where both teams work through priorities, API shapes, and AppShell convergence. The rows below are its actionable children.

> **The contribute-back loop (standing rule).** Before hand-rolling any console UI:
> **(1)** check `@protolabsai/ui` for an existing component — adopt it (the Alert
> banners were re-implemented twice before anyone looked); **(2)** if it's genuinely
> missing and the pattern has (or will have) a second consumer, **file a gap issue on
> protoContent** in the #131/#186/#188 format (Gap / Evidence / Proposed API /
> Priority / Context) and link it here; **(3)** an app-local interim implementation is
> fine — mark it with the issue # so the adoption sweep can retire it when the DS
> ships. Every bespoke component is either a filed gap or a deliberate
> "not-for-the-DS" entry — never silent.

**`@protolabsai/ui@0.5.0` (published 2026-06-08) delivered 3 of 6** — the P0 + both P1 composites. Adoption now unblocked.

| # | Request | Priority | Status |
|---|---|---|---|
| [#131](https://github.com/protoLabsAI/protoContent/issues/131) | `Menu` / `DropdownMenu` (Radix, open-at-coords) | P0 | ✅ **Shipped 0.5.0** — `Menu` forwardRef + `MenuHandle.open({x,y})`, `MenuItem/Separator/Label/Sub` |
| [#132](https://github.com/protoLabsAI/protoContent/issues/132) | `PanelHeader` composite | P1 | ✅ **Shipped 0.5.0** — exact `{title,kicker,actions,compact}` API |
| [#133](https://github.com/protoLabsAI/protoContent/issues/133) | `Tabs` icon+badge slots | P1 | ✅ **Shipped 0.5.0** — `TabItem.icon?` + `.badge?` |
| [#134](https://github.com/protoLabsAI/protoContent/issues/134) | `ScrollArea` min-height:0 + overscroll + focus ring | P2 | ✅ **Shipped 0.6.0** |
| [#135](https://github.com/protoLabsAI/protoContent/issues/135) | `Button` variants (ghost/danger) + icon-only + size | P1 | ✅ **Shipped 0.6.0** |
| [#136](https://github.com/protoLabsAI/protoContent/issues/136) | `Skeleton` loading-placeholder primitive | P1 | ✅ **Shipped 0.6.0** |

**Entire original wishlist (#131–#136) delivered across 0.5.0 + 0.6.0.** AppShell convergence agreed (#137); sub-pieces now filed as children:

| # | AppShell sub-piece | Status |
|---|---|---|
| [#142](https://github.com/protoLabsAI/protoContent/issues/142) | `SurfaceRail` (icon rail + tab strip) | Filed — source handed over; offered to PR |
| [#143](https://github.com/protoLabsAI/protoContent/issues/143) | `MobileNav` (bottom quick-bar + drawer) | Filed — source handed over; offered to PR |
| [#144](https://github.com/protoLabsAI/protoContent/issues/144) | `AppShell` composite (controlled; app keeps persistence) | Filed |

Stack decision (UI team, on #137): **Radix for hard interactive primitives (Menu now; Popover/Combobox/Select later) styled with `--pl-*`; everything else className-only; no Tailwind.**

| # | Request | Priority | Status |
|---|---|---|---|
| [#218](https://github.com/protoLabsAI/protoContent/issues/218) | `Tabs` `segmented` variant / `SegmentedControl` (two-level nav scope toggle) | P2 | **Shipped in ui 0.28.0, adopted 2026-06-12** — the settings **Host / App \| Workspace** home toggle (`SettingsSurface.tsx`) and the MCP add-server **Form \| Paste JSON** toggle (`McpPanel.tsx`, the last `.segmented` hand-roll) both use `<Tabs variant="segmented">`; the local `.segmented` CSS block is deleted. |
| [#224](https://github.com/protoLabsAI/protoContent/issues/224) | `plugin-kit.js` classic-`<script>` contract is impossible (the file is ESM — `Unexpected token 'export'`, the `window.protoPluginView` global never sets) | P2 | Filed 2026-06-12. **App-side fixed** (notes-adopts-kit PR): the notes editor + `chat_example` + both plugin-view docs now load the kit via dynamic `import(base + "/_ds/plugin-kit.js")` from a module script. |
| [#225](https://github.com/protoLabsAI/protoContent/issues/225) | `SideNav` — vertical section navigation (`Tabs` is horizontal-only; the settings Workspace home's 11 sections overflowed/read as "intense" in a strip) | P2 | **Shipped in ui 0.30.0 (PR #227), adopted 2026-06-13** — `SettingsSurface` uses `<SideNav>` (scope toggle in its `header` slot + sections as the rail); the interim `.settings-sidenav*` rail + CSS are retired. We omit `responsive` (its 15rem collapse-to-`<select>` is wider than our compact in-rail column, so it'd render a dropdown not the vertical nav). Collapsible field groups use the `Accordion`/`AccordionItem` shipped in 0.29. |

## Console adoption status (branch `ds-adoption-sweep`, 2026-06-09)

Sharing standard: **`docs/design/component-sharing-standard.md`** (the contract). Each row is a held commit; all green (68/68 e2e, tsc+build).

**Adopted (local retired + CSS removed) — 18 held commits, all 68/68 e2e green:**
- `PanelHeader` → DS (19 surfaces) · `StageSubnav` → DS `Tabs` · context menu → DS `Menu` (local `dropdown-menu` deleted) · `ScrollArea` + `ConfirmDialog` → DS · `StatusPill` → DS `Badge` (adapter) · `PanelSkeleton` spinner → DS `Spinner`.
- **`Button` — DONE.** All ~90 buttons (14 clean files batch + 5 mixed files per-button) → DS `Button` (`variant`/`icon`); bespoke buttons (chat-tab, slash, modes) kept. **All bespoke button CSS removed (−120 lines of `theme.css`).**
- Bumped `@protolabsai/ui` `^0.4.0` → **`^0.8.0`**; all imports on domain **subpaths** (root barrel gone).
- Fixed: always-mounted DS `Menu` must render **outside** the `.app-shell` grid (stray anchor row → footer overlap).

- **Forms — DONE:** `Input`/`Select`/`Textarea` (16 files) + `Checkbox` (4 inline bool fields) + `Table` (telemetry) + `Callout` (bare-text setup notices). All 68/68 green.

**Componentization complete to the extent the DS supports it.** Every component with a clean DS mapping is adopted. The remainder is documented exceptions, not unfinished work:
- **Acceptable native patterns** (per the sharing standard): ~89 simple `title=` tooltips stay native (DS `Tooltip` is for rich content); structured alert composites (panel-error retry card, settings-banner icon+restart) stay app-side (DS `Callout` has no icon/action slot — swapping is a redesign); the chat composer `<textarea>` stays raw (needs a `ref`; DS forms aren't `forwardRef`).
- **Blocked on a DS gap** (filed): 2 external-label `<label htmlFor>` toggles stay raw — DS `Checkbox`/`Switch` take no `id`/`...rest` → [protoContent #155](https://github.com/protoLabsAI/protoContent/issues/155).
- **`SurfaceRail`/`MobileNav`/`AppShell` — deferred by design:** the local rail/nav are already DS-API-identical (we authored the upstream from them) + zero-coupling; swapping is low-value/high-risk (shell-grid + ~30 `.rail` e2e refs). Part of the controlled AppShell convergence (#137) — and the new pluggable utility-bar (ADR 0046) lands there too.

## Full-sweep classification (all 323 `theme.css` class groups triaged)

Every reusable widget class was cross-referenced against installed `@protolabsai/ui@0.4.0` source. Three buckets:

- **DS gap → filed:** Menu (#131), PanelHeader (#132), Tabs icon+badge (#133), ScrollArea parity (#134), Button variants (#135), Skeleton (#136). AppShell (rail shell / resize-handle / mobile-drawer) held for live coordination.
- **Covered by 0.4.0 → adopt, not filed:** Button (default/primary), Badge (chips/priorities/states), StatusDot (dots), Card (all `*-card`), Dialog (confirm/schedule/mcp-add modals), Drawer (mobile), Field/Input/Textarea/Select/Switch/Checkbox (all form rows), Callout (panel-error/settings-banner/errors), Stat/Stats (metrics), Table (telemetry/lists), Tooltip (89 native `title=`), Spinner (`.spin`/loaders), Empty (empty-states), Divider, Kbd, Prose.
- **App-specific → stays local:** markdown renderer, chat tool-renderers (`.tool-*`), chat tabs (closeable/editable sessions), slash command menu (`.slash-*` — chat autocomplete), setup wizard stepper (`.setup-*` — one-off onboarding), workflow builder canvas, plugin-iframe host, intro splash, activity/inbox/tasks domain rows. `.segmented` (2 uses) → suggested as a `Tabs variant="segmented"` on #133.

**Conclusion:** the DS gap surface is closed at #131–#136 + AppShell-held. Everything else the console needs is either already in 0.4.0 (an adoption sweep, task #68) or legitimately app-specific.

## Summary for the UI team

- **Build:** `Menu`/`DropdownMenu` (P0), `AppShell` convergence on our dual-rail shell (P0), `PanelHeader` (P1), `Tabs` icon+badge slots (P1).
- **We hand over proven implementations** for all four (Radix dropdown, rail shell, PanelHeader, StageSubnav).
- **Everything else** the console needs already exists in 0.4.0 — it's an adoption sweep (task #68), not new DS work.
- **Principle:** componentize + prove in protoAgent → extract upstream. The four above are already proven here.
