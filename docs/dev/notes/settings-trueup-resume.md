# Settings/views/config true-up — resume guide

Cold-start guide for the two remaining stages of the settings/views/config true-up.
The canonical plan + ledger is **ADR 0048 §6** (`docs/adr/0048-settings-ia-two-scope-homes.md`).
This file is the "where to start" companion.

## Status (2026-06-30)

The **app-only true-up is complete and merged** (#1428–#1434):

| PR | What |
|----|------|
| #1428 | identity name → `/api/settings`; operator-clobber bug; fleet `plugins.enabled` → dedicated endpoint (T1–T3) |
| #1429 | ADR 0048 §6 — canonical-system contract + T-ledger + DS-extraction plan |
| #1430 | Skills surface self-toasts — silent-failure bug (T4) |
| #1431 | toast-convention sweep: MCP · Plugins · Knowledge (T5) |
| #1432 | plugin uninstall/restart → DS `ConfirmDialog` (T7a) |
| #1433 | deleted the dead `hostLayer` path from `SettingsCategory` |
| #1434 | DS `TextLink` + `Kbd` adoption (T7b) |

**The one system is real and enforced:** every config surface saves through the schema-cascade
`/api/settings` (Field registry → `build_schema()` → `SettingInput` → ADR 0047 cascade). Reuse via
`QuickSetting` (chip/dialog) and `PluginSettingsDialog` (Dialog wrapping `SettingsCategory`).
Bespoke managers are legit where they're rich CRUD / device-local, but must (a) save through the
canonical path, (b) report via DS `useToast`, (c) use DS form primitives.

## Stage A — T6: Playbooks + Knowledge → TanStack Query

**What:** `PlaybooksSurface` (`apps/web/src/playbooks/PlaybooksSurface.tsx`) and `KnowledgeStore`
(`apps/web/src/knowledge/KnowledgeStore.tsx`) are the last two surfaces on the **retired manual-fetch
pattern** (`useState` + `useEffect` + `try/catch` + `load()`). Migrate them to TanStack Query.

**Risk/value:** user-INVISIBLE internal refactor of two ~350-line CRUD components — real regression
risk, no user-facing benefit. Do it carefully, one component per PR, full e2e each time.

**Reference pattern:** `apps/web/src/app/GoalsPanel.tsx` — `useSuspenseQuery(goalsQuery())` for reads,
`useMutation` + `qc.invalidateQueries` for writes, no `useEffect`/busy-flag/try-catch. Queries are
defined in `apps/web/src/lib/queries.ts`.

**Concrete steps (per component):**
1. Add a query factory to `lib/queries.ts` (e.g. `playbooksQuery()` → `api.playbooks()`,
   `knowledgeQuery(query)` → the search call). Mirror `goalsQuery`.
2. Replace the manual list state + `load()` + `useEffect` with `useSuspenseQuery`.
3. Convert each action (save / promote / unshare / delete / ingest) to a `useMutation` that
   invalidates the query on success. Errors already toast (T4/T5 work) — keep that.
4. **Verify the render site has a Suspense + ErrorBoundary** above it. `SettingsCategory` already
   uses `useSuspenseQuery`, so `SettingsSurface ▸ Skills` likely has a boundary — confirm before
   relying on it; Knowledge is an App-level rail surface (`App.tsx` `case "knowledge"`), check its
   boundary too.
5. Drop the now-dead loading/error state.

**e2e gotcha:** `playbooks.spec.ts` and `mcp.spec.ts` mutate the **shared in-memory mock**; they run
`test.describe.configure({ mode: "serial" })` + a `beforeEach` reset. Keep that intact — TanStack
caching can change request timing and re-expose ordering issues.

## Stage B — Phase 4: DS extraction (cross-repo)

**Where:** the design system is `@protolabsai/ui`, checked out at **`/Users/kj/dev/protoContent`**.
The installed/hoisted copy is at `/Users/kj/dev/protoAgent/node_modules/@protolabsai/ui` (ships
`src/*.tsx`), version **0.48.1**. The app declares `^0.48.3` in `apps/web/package.json`.

**The loop for each item:** build in protoContent → PR there → cut a DS release → bump
`@protolabsai/ui` in the app → adopt in-app → delete the hand-rolled code. File gaps with
Gap/Evidence/API/Priority per the standing contribute-back rule.

**Rule:** the settings *business logic* (save cascade, ADR-0047 inheritance, `depends_on` visibility)
stays app-owned. Only the **primitives** move upstream.

**Items (priority order):**
1. **`ToastProvider position` prop** — protoContent **#348 is merged but NOT in 0.48.1**
   (`ToastProvider` there is `({children, max})`, no `position`). So this needs a **DS release first**,
   then: bump dep → `<ToastProvider position="top-right">` in `apps/web/src/main.tsx` → delete the
   `.pl-toast-stack` top-right override in `theme.css`. Smallest, do first to prove the loop.
2. **`Button loading` prop** (spinner + `disabled` + `aria-busy`) — biggest app reduction: ~16 files,
   42 `Loader2`, the app `.spin` keyframe (`theme.css`), and the `RefreshButton`/`TestConnectionButton`
   wrappers in `apps/web/src/app/ui-kit.tsx`.
3. **`SegmentedControl`/`ChipGroup` + icon-adorned `Input`** — needed for the catalog raw search +
   category chips in `McpCatalogDialog` + `PluginsSurface` DiscoverTab. (Reclassified here from T7:
   DS 0.48.1 has no chip/segmented primitive and `Input` takes no leading icon — so these are DS
   *additions*, not adoptions.)
4. **Headless context-menu kit** — move `apps/web/src/contextMenu/{registry,store,ContextMenuRenderer,types}`
   (~120 LOC) to DS, rendering DS `Menu` (which already has `open({x,y})`). App keeps domain
   `registrations.tsx`. Also add `TabBar onTabContextMenu`.
5. **`FieldControl` (type→control resolver) + `PropertyRow` + `SecretInput(isSet)`** — extract the
   `SettingInput` switch (`SettingsCategory.tsx`) as DS primitives; orchestration stays app.

## Working rules (don't relearn the hard way)

- **Worktrees only** — never develop in the primary checkout or the settings tree. `git worktree add
  ../pa-<task> -b <branch> origin/main`; symlink `node_modules` (root **and** `apps/web`) from the
  main checkout for fast tsc/build/test; `git worktree remove --force` when merged.
- **Run the FULL e2e suite locally before pushing** — CI's "Web E2E smoke" runs all of
  `npx playwright test` (not just touched specs). A subset-only run missed a fleet spec once.
- **Toast e2e:** always `page.locator(".pl-toast", { hasText: ... })` — a bare `.pl-toast` collides
  with the mock's seeded "Goal achieved" toast (`events.spec.ts`) → strict-mode failure.
- **Gates:** `apps/web` → tsc + `vite build` + `npx playwright test` + `npx vitest run`; Python →
  `ruff check .`, `lint-imports`, `pytest tests/ -q` (see PROTO.md).
