# ADR 0086 — Chat-first mobile shell (amends ADR 0035 D6)

**Status:** Accepted

**Amends:** [ADR 0035 — Console layout: symmetric dual-rail, mobile-first, persisted UI
state](0035-console-layout-dual-rail-mobile-first.md), decision **D6** only. D1–D5 and D7
(the desktop dual-rail split, swappable surfaces, unified rail style, resize handle,
persisted UI state, DS/theming) are unchanged.

## Context

ADR 0035 D6 specified mobile as:

> **Mobile:** a single surface at a time. A **bottom quick-access bar** of 4–5 surfaces
> (app-style bottom nav) + a **hamburger** for the full surface list. No split screen on
> mobile. […] Breakpoint-driven: **the same surfaces + store, a different shell.**

That shipped and it works. But "the same surfaces + store, a different shell" is a
*responsive* model, not a *native* one, and the result reads as a shrunken desktop console:
threads switch through a `<select>`, chat is one tab among co-equal surfaces, and the
information hierarchy is the desktop's.

A July 2026 audit of the console and `@protolabsai/ui` also found that the layer beneath
the IA — touch input — was essentially absent. Verbatim counts at the time:

| Signal | Count |
|---|---|
| Form controls at ≥16px | 0 (every input zoomed on iOS focus) |
| `visualViewport` usage | 0 (the keyboard covered the composer) |
| `-webkit-tap-highlight-color` | 0 |
| `touch-action: manipulation` | 0 |
| `:active` press states | 0 |
| `env(safe-area-*)` consumers | 1 |

Three live defects in the D6 shell itself:

1. The default quick-bar (`uiStore.quickBar`) still listed `plugins`, a surface folded into
   Settings — `MobileNav` filters unresolvable ids, so **fresh installs got a 2-tab bar**.
2. `openView` (⌘K, the rail context menu, plugin `ui.navigate`, launcher intents) wrote the
   per-dock surface id, but the mobile stage renders from `mobileActive` — so **every
   programmatic navigation silently did nothing on a phone**.
3. `toggleQuickBar` had **zero callers**: D6's promised user-configurable quick-bar was
   never built.

## Decision

### D1 — Chat is the root view, not a tab

On mobile, chat **is** the app. It is the root and never leaves the screen. Every other
surface (Work, Knowledge, Memory, plugin views) is **pushed over** it and dismissed with a
back affordance. The bottom quick-bar is **retired** on mobile.

Consequences: `quickBar` / `toggleQuickBar` become desktop-irrelevant dead state (D6's
configurable quick-bar is formally withdrawn rather than left as an unbuilt promise), and
`mobileActive` is reinterpreted as a one-level navigation stack — `"chat"` means the root,
any other value means that surface is pushed.

One level is deliberate. A deeper stack implies a breadcrumb model the surfaces don't have,
and every surface is reachable in one hop from the drawer.

### D2 — Sessions switch through a sheet, not a form control

The DS `TabBar`'s `responsive` collapse (a `<select>`) is suppressed on mobile. The shell's
header carries the **session title**, and tapping it opens a bottom sheet listing threads.
Bottom-anchored because that is where the thumb is.

The sheet is hand-rolled: the DS `Drawer` supports `side: "left" | "right"` only. Filed as a
DS gap; it mirrors `AppDrawer`'s structure so it can be swapped for a DS bottom sheet later.

### D3 — The mobile shell is app-owned, not the DS AppShell

The DS `AppShell`'s mobile branch is a hard early return gated on
`isMobile && mobileItems && onMobileSelect && quickBarIds`, and it renders `leftContent`
plus a `MobileNav` — dropping those props to escape it falls through to the **desktop**
tree. **The DS cannot express chat-as-root.** Below 768px the console therefore renders its
own `MobileShell` instead of `AppShell`.

This is a deliberate, bounded exception to the DS-first rule ([ADR 0037]): the DS still owns
every *component* inside the shell (composer, conversation, overlays, buttons) and still
drives every desktop viewport. Only the mobile *shell* is ours. Revisit if the DS grows a
chat-first shell primitive.

Two consequences worth recording, both of which were live bugs during implementation:

- **The DS early return also drops `utilityBar`** (it renders after the branch). Settings,
  the activity widget, and the layout toggles never existed on mobile. Settings reaches the
  phone through the drawer; fleet identity moved into the drawer header, since the
  chat-first header carries the session title rather than the DS `Header`.
- **`.chat-stage` is `grid-template-rows: auto minmax(0,1fr)`** where the `auto` row was the
  tab bar. Removing the tab bar (D2) without collapsing the grid leaves the session pool in
  the `auto` row sizing to content and the `1fr` row empty — the composer floats mid-screen
  over dead canvas. This is the #1899 failure one layer up; the mobile shell collapses the
  grid to one row.

### D4 — The streaming-continuity contract is load-bearing (#613)

A conventional push/pop navigator that swaps the root out for the pushed view would unmount
`ChatSurface` mid-turn and drop the SSE stream. So pushed views **layer over** a persistent
root; they never replace it.

The root holds the chat slot **and** every docked background plugin view (iframes that must
not remount). A background view can itself be the active surface — shown by display toggle,
with no pushed layer — which is why the shell applies `inert` on *"something covers the
root"*, never on *"the back affordance is showing"*.

### D5 — Keyboard handling is two mechanisms, not one

The shell is a fixed `100dvh` column under `body { overflow: hidden }`. `dvh` tracks browser
chrome but **not** the keyboard, which iOS overlays rather than resizing around.

- `interactive-widget=resizes-content` (viewport meta) — Chrome 108+ shrinks the layout
  viewport itself.
- `useKeyboardInset` (`visualViewport` → `--kb-inset`, subtracted by `.app-shell`) — the iOS
  Safari path, which ignores that directive entirely.

Both are required; they compose, since whichever engine already handled it reports ~0. The
hook also re-pins conversation scrollers, because the DS `Conversation` observes its
*content* element and a keyboard doesn't change content height.

### D6 — Touch input has a floor, and it is tested

16px minimum on any focusable control (below it iOS zooms on focus — a threshold, not a
design choice; `user-scalable=no` is rejected as it costs pinch-zoom accessibility). 44px
minimum hit area, which may be an `::after` overlay so visual density is preserved.
Tap-highlight suppressed, `touch-action: manipulation`, real `:active` press states.

Most of these rules target DS-owned classes, so they live in a **temporary**
`mobile-native.css` override layer with every block tagged `[DS]` or `[APP]`, retired as the
corresponding `@protolabsai/ui` fixes land. **Two e2e specs assert the floor** (no control
under 16px, no target under 44px) so it cannot silently regress — which is how it was lost
the first time.

## Consequences

- Mobile and desktop are now genuinely different shells sharing surfaces and state, rather
  than one shell at two widths. Surface authors are unaffected: a surface is still a
  component that fills its container.
- The 768px breakpoint is now a **shell switch**. It must stay in lockstep across
  `lib/useIsMobile.ts`, the DS `mobileBreakpoint`, and the `@media` blocks.
- The tablet band (768–1024px) still gets the desktop shell, unchanged — consistent with the
  2026-07-07 decision to leave it as-is.
- `quickBar` state remains in the store for now (persisted, harmless) but is unread on
  mobile. Removing it is a separate cleanup.
