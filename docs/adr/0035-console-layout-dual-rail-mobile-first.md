# ADR 0035 — Console layout: symmetric dual-rail, mobile-first, persisted UI state

**Status:** Proposed

## Context

The console's layout grew organically: a **left rail** of surfaces (Chat, Activity, Studio,
Knowledge, Plugins, Settings) + a **fixed right panel** with a different paradigm (segmented
tabs: Notes, Beads, Goals, Schedule, plus plugin right-panels). Two different navigation idioms,
an asymmetric layout, a thin resize handle, no mobile story, and **UI state lives in React
`useState`** — so a refresh drops you back to defaults (wrong surface, wrong tab).

We want to **lock the layout paradigm** before building more on it — specifically before ADR
0034's plugin-UI SDK codifies layout primitives and before Notes is ported to a React plugin
(0034 slice 4). This ADR is that lock-in.

## Decision

### D1 — Symmetric dual rails (split screen)

The right side **mirrors** the left: both are **rails** hosting surfaces. The console is a
**split screen** — one surface on the left, one on the right (e.g. Chat ‖ Notes) — with a
draggable divider. Same rail component, same interaction, both sides.

- Left rail (default): Chat, Activity, Studio, Knowledge, Plugins, Settings.
- Right rail (default): Notes, Beads, Goals, Schedule, Plugins.

The old "right panel with its own tab style" is gone — it becomes a right *rail* identical to
the left.

### D2 — Surfaces are swappable between rails

A surface isn't bound to a side. The user can **move any available surface to either rail**
(its default side is just a default). So you can put Beads on the left and Chat on the right if
that's your workflow. Assignment is per-user, persisted (D5).

**A surface lives on exactly one side at a time** (resolved) — moving it to a rail removes it
from the other. This avoids duplicate-state surfaces (two live Chats) and keeps the model simple:
the split shows two *different* surfaces.

### D3 — One unified tab/rail style

Adopt **one** navigation style everywhere — the current right-panel **segmented** look — for
both rails and for in-surface view-tabs. No more "left-rail icons" vs "right-panel segments" as
two idioms. One component, one look, top to bottom.

### D4 — A real resize handle

Widen the divider into an easy left↔right **drag handle** (generous hit area, clear affordance,
keyboard-resizable, double-click to reset). Min/max widths per rail; collapse a side to give the
other full width.

### D5 — Persisted UI state (Zustand)

Introduce a **Zustand store** (`+ persist` middleware → localStorage) as the single source of
truth for layout/UI state: active surface per rail, active in-surface tab, rail width / collapse,
per-rail surface assignments, and the mobile quick-bar slots. **A refresh restores exactly where
you were.** (Server data stays in react-query; this store is *UI* state only — they don't mix.)

### D6 — Mobile-first

Design the layout mobile-first, then enhance to the dual-rail split on wider viewports:

- **Mobile:** a single surface at a time. A **bottom quick-access bar** of 4–5 surfaces (app-style
  bottom nav) + a **hamburger** for the full surface list. No split screen on mobile.
- **Desktop:** the dual-rail split (D1) with the resize handle (D4).
- Breakpoint-driven: the same surfaces + store, a different shell.

**The quick-bar is user-configurable** (resolved) — the user picks which surfaces fill its 4–5
slots from the available set, persisted (D5). Sensible default (e.g. Chat, Activity, Knowledge,
Plugins). **A plugin needs no special slot mechanism** — plugin views are surfaces like any
other, so pinning one to the quick-bar is the same action as pinning Chat.

### D7 — Design-system + theming pass

Alongside the layout: consolidate the theme tokens, tighten spacing/typography/components, and
make theming first-class (so the ADR 0034 plugin-UI SDK can expose a *stable* token set to
remotes). Scope to be detailed as its own slice — this ADR commits to doing it with the layout,
not to a specific token list yet.

## Consequences

- **One coherent paradigm** — symmetric rails + one tab style + persisted state is simpler to
  reason about (and to document) than today's two idioms.
- **Plugins get a real home on either side** — a plugin surface is a first-class rail citizen, and
  (0034) a `ui: react` one mounts natively. This is *why* we lock layout before the SDK.
- **Mobile becomes a real target**, not an afterthought — but it's net-new shell work + testing.
- **A new state dependency (Zustand)** — small, but it owns UI state going forward; we must keep
  the line between it (UI) and react-query (server data) clean.
- **Migration:** the right-panel → right-rail change touches `App.tsx` heavily; existing plugin
  views (rail + right) keep working (their `placement` maps onto a default rail side).
- **Sequencing:** this lands *before* ADR 0034 slices 2–4 so the SDK + the Notes port target the
  final layout.

## Build order (proposed slices)

1. **UI-state store** — Zustand + persist; migrate active-surface/tab/width out of `useState`.
   Refresh-restores. (Foundational, low-risk, independently shippable.)
2. **Symmetric rails + unified tab style** — generalize the rail component; right panel → right
   rail; one segmented style; surfaces render either side off the store.
3. **Swap + resize** — move-surface-between-rails control; the widened/keyboard/reset handle.
4. **Mobile shell** — bottom quick-bar (+ plugin slot) + hamburger; breakpoint switch; no split.
5. **Design-system + theming pass** (D7) — token consolidation; feeds the 0034 SDK token set.

Then resume ADR 0034 slices 2–4 (plugin-UI SDK → trust gate → port Notes) on the locked layout.

## References

- ADR 0026 (plugin console surfaces — rail views + right panel, the thing this generalizes),
  ADR 0034 (plugin UI as first-class React — its SDK/Notes port target this layout).
- Zustand (+ `persist` middleware) for UI state; react-query stays for server state.
