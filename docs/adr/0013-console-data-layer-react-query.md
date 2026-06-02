# ADR 0013 — Console data layer: TanStack Query + Suspense + ErrorBoundary

- **Status:** Accepted (2026-06-02) — foundation + first surface (Goals) shipped; remaining surfaces migrate in follow-up PRs
- **Date:** 2026-06-02
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** console, frontend, dx
- **Supersedes / Superseded by:** —

> Every surface in the React operator console fetched the same way by hand: a
> `useEffect` that calls the API, a `busy` boolean, a `try/catch` that funnels
> into a single global `setError` banner, and — for live data — a hand-rolled
> `setInterval` poll. That's a lot of identical plumbing per surface, the error
> story is one app-wide banner with no per-panel retry, and loading is ad-hoc.
> We adopt **TanStack Query** (v5, suspense mode) as the console's data layer:
> reads are `useSuspenseQuery`, loading is a **`<Suspense>`** fallback, failures
> are caught by an **`<ErrorBoundary>`** with a contained retry, and mutations
> are `useMutation` that invalidate the relevant query key. Live surfaces use
> `refetchInterval` instead of bespoke polls.

---

## 1. Context & problem

The console (`apps/web`) has ~10 read surfaces — runtime status, subagents,
notes, beads, goals, schedules, workflows, telemetry, playbooks, settings,
activity/inbox. Each independently reimplements:

- **Loading**: a `*Busy` flag toggled around the fetch, rendered as a spinner.
- **Errors**: `catch (e) { setError(...) }` → one global banner, no retry, no
  per-panel containment (a goals fetch failure looks like a chat failure).
- **Freshness**: surfaces that must track agent-side changes (notes the agent
  writes, goals it advances) hand-roll a `setInterval` with their own
  "don't refetch while busy/dirty" guards.
- **Refetch on navigate**: a manual refresh button, or a re-fetch in an effect
  keyed on the active tab.

This is boilerplate-heavy, inconsistent, and puts all error UX in one banner.

## 2. Decision

Introduce **`@tanstack/react-query`** as the single data layer for console
reads, with the canonical Suspense pattern:

- A single **`QueryClient`** (`lib/queryClient.ts`) at the app root via
  `QueryClientProvider` (`main.tsx`). Defaults: `staleTime: 5s`,
  `retry: 1`, `refetchOnWindowFocus: false` (a local operator console, not a
  long-lived dashboard).
- **Reads** use `useSuspenseQuery(options)`. Loading suspends to a
  `<Suspense fallback={<PanelSkeleton/>}>`; a thrown fetch error is caught by an
  **`<ErrorBoundary>`** (`app/ErrorBoundary.tsx`) wired to
  `<QueryErrorResetBoundary>` so its retry re-runs the query. The fallback is a
  contained `<PanelError>` card with a Retry button — no global banner.
- **Query options** live in `lib/queries.ts` (stable, hierarchical query keys +
  `queryOptions(...)` factories), so a mutation can invalidate a whole subtree.
- **Mutations** use `useMutation` and invalidate the matching key on settle.
- **Live surfaces** set `refetchInterval` on the query (e.g. goals every 5s)
  instead of a manual `setInterval`.

### Not in scope / kept as-is

- **Notes** stays imperative. Its panel owns local edit state (dirty tracking,
  per-tab undo history, debounced autosave, "adopt newer server version without
  clobbering unsaved edits") that a read-cache doesn't model. It keeps its
  bespoke load/poll but is wrapped in the same `<ErrorBoundary>` for error
  containment. We can revisit once the read-only surfaces are migrated.
- **Streaming chat** (A2A SSE) is not a query; unchanged.

## 3. Migration plan (slices)

Console-wide, but shipped incrementally so each PR is reviewable + green:

1. **Foundation + Goals** (this ADR's PR) — add the dep, `QueryClientProvider`,
   `ErrorBoundary`/`PanelError`/`PanelSkeleton`, `lib/queries.ts`, and migrate
   the **Goals** sidebar panel as the reference implementation (extracted to
   `app/GoalsPanel.tsx`).
2. **Beads** sidebar panel (status + issues reads → suspense; create/update/
   close/delete → `useMutation`).
3. **Studio** surfaces — subagents, workflows, run.
4. **System** surfaces — runtime status, telemetry, settings.
5. **Activity** — thread, inbox, schedules.

Each slice deletes the surface's `useEffect`/`busy`/`try-catch`/poll and routes
its errors through a local boundary.

## 4. Consequences

**Positive** — far less per-surface boilerplate; consistent, declarative
loading + per-panel error/retry; free caching, dedup, and background refetch;
`refetchInterval` replaces hand-rolled polls; surfaces shrink (logic moves into
small panel components + query factories), trimming the 1600-line `App.tsx`.

**Negative / costs** — a new runtime dependency on the public template
(~`react-query`, modest bundle increase); a second rendering concept
(Suspense/boundaries) contributors must understand; the migration spans several
PRs during which the console mixes both patterns; notes remains the imperative
exception.

## 5. Related

- [React + Tauri console](/guides/react-tauri-ui)
- [ADR 0009 — Studio control stack](/adr/0009-studio-control-stack)
- [ADR 0003 — Reactive agent & Activity thread](/adr/0003-reactive-agent-activity-thread) (the event stream that drives some live surfaces)
