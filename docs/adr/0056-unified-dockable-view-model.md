# ADR 0056 — Unified dockable-view model (tabs ↔ rails)

- **Status:** Proposed (2026-06-17)
- **Date:** 2026-06-17
- **Deciders:** Josh Mabry; protoAgent maintainers
- **Tags:** ux, layout, appshell, dnd, information-architecture, uiStore, chat
- **Related:** builds on the DS AppShell rail reorder (`onRailReorder`, dnd-kit)
  and the new DS `TabBar` `onReorder` (protoContent #264 — step 1 of this
  direction). Reframes the surfaces from [ADR 0042](./0042-fleet-supervisor-unified-console.md)
  (console IA, slug routing), [ADR 0044](./0044-plugin-driven-console-navigation.md)
  (plugins contribute rail surfaces), [ADR 0045](./0045-chat-panel-slot.md) (the
  chat panel is a slot), and [ADR 0048](./0048-settings-ia-two-scope-homes.md).
  Touches `state/uiStore.ts` (`railOrder`/`surface`) and `chat/chat-store.ts`
  (`sessions`).

> A validated POC (protoContent branch `spike/dock-dnd-tabs`, Storybook
> *Spikes/Dock DnD*) showed three gestures that feel right: **reorder** tabs in a
> panel, **tear a tab out** to a rail as a top-level panel, and **drag a rail
> panel in** as a tab. The DnD itself is a non-issue — dnd-kit already ships in
> the DS, the rails already reorder, and tab reorder is landing in #264. The
> blocker is the **model**: today a "tab" (a chat session in `chatStore`) and a
> "rail item" (a surface in `uiStore.railOrder`) are *two different types in two
> different stores*, so tear-out/re-dock means converting between them and there's
> no shared view identity to move. This ADR proposes one **dockable `View`** type
> that can live in either container, with a single layout store — the unification
> the spike proved is what makes the gestures clean.

## 1. Context & problem

The console has two placement systems that look similar on screen but are
unrelated underneath:

1. **Rails hold *surfaces*.** Left/right/bottom rails render id-addressable
   views; order lives in `uiStore.railOrder = { left, right, bottom }` and
   reorders today via the DS AppShell `onRailReorder` (dnd-kit, ADR 0035/0036
   context menu as the keyboard path). Surfaces are core (`chat`, `activity`,
   `box`, `settings`, …), plugin (`plugin:<id>:<view>`, ADR 0044), and fork
   (`src/ext` registry). A surface is a **top-level panel**.

2. **Tabs hold *chat sessions*.** `ChatSurface` renders a `<TabBar>` of
   `chatStore.sessions` *inside* the single `chat` surface. A session is a
   **sub-item of one surface**, persisted separately
   (`protoagent.chat.sessions[:slug]`), not a peer of surfaces. (The
   Activity/Plugins/Box sub-tabs are a third thing again — single active-id
   strings in `uiStore`, fixed sets.)

So "tab" and "rail item" are different types, in different stores, with different
persistence and lifecycles. Two consequences block the UX:

- **No shared identity to move.** Tear-out (tab→rail) and re-dock (rail→tab) are
  conversions between `chatStore.sessions` and `uiStore.railOrder`, not a move of
  one object. Every gesture becomes a special case.
- **A session isn't a standalone surface.** It's one of N inside the `chat`
  surface. "Tear this session out to the rail" has no referent today — there's no
  notion of a session existing as its own top-level panel.

The POC sidesteps both by giving every item one `View` type that lives in either
container. That's the missing piece, and it's a model decision, not a DnD one.

## 2. Decision

Adopt a **unified dockable-view model**:

- **One `View` type** — `{ id, kind, title, icon }`, `kind ∈ { surface, session,
  plugin, ext }`. The *content* stays in its existing registry (CORE_SURFACES,
  `chatStore.sessions`, plugin views, `src/ext`) and is resolved **by id**; the
  View is just the addressable, placeable handle.
- **One `DockLayout`** — the ordered placement of view-ids across containers,
  owned by a single layout store, persisted + migrated:
  - **Containers (v1):** the three rails (top-level panels, as today) + the main
    panel's **tab strip** (ordered view-ids + active). Multi-panel splits are
    explicitly **out of v1** (see §5/§8).
  - A `View` can sit in any container; the three gestures are container→container
    moves of a view-id (exactly the POC).
- **One shared `DndContext`** spanning the rails and the tab strip, so a drag can
  cross between them. (Today rails and `TabBar` each own a *separate* context —
  unifying them is the main DS lift; see §4.)

Net: tab reorder, tear-out, and re-dock are the same operation — move view-id X
from container A at index i to container B at index j — with a small rules layer
for forbidden drops.

## 3. The crux: session-as-View

The hard, non-mechanical part is promoting a **chat session to a first-class
View** (`kind: "session"`). That generalizes the chat slot (ADR 0045) from "one
`chat` surface containing N sessions" to "N session-views placeable anywhere":

- Tearing a session to a rail = it becomes a **top-level panel** showing that one
  session.
- The current `chat` surface becomes a **panel whose tab strip holds the
  session-views** — i.e. the default container, not a special case.
- This unlocks a real workflow win: **two sessions side by side** (one torn into
  the right rail), which is impossible today.

Invariants that must survive the promotion:

- **Chat is forbidden in the bottom dock** (streaming slot; `App.tsx` bounces it
  back today). Encode as a per-`kind` placement rule, not a hardcode.
- **Always-mounted chat contract** (ADR 0045 / #613) — session-views must keep
  their mount semantics so a streaming turn isn't unmounted by a move.
- **Plugin placement** (`placement: rail|right|bottom`, ADR 0044) and **plugin
  dots** (ADR 0039) keep working as view metadata.

## 4. Model & state

- **View resolution.** A registry façade `viewFor(id) → View` over the existing
  sources (no data migration of the sources themselves).
- **Layout store.** Two paths:
  - **(B) Bridge first (recommended).** Keep `uiStore` + `chatStore`; add a thin
    `dockLayout` layer that references both by id and owns cross-container order +
    active. Lowest risk; ships incrementally.
  - **(A) Consolidate later.** Fold placement into one `dockStore` once B proves
    out. Bigger migration; defer.
- **Persistence.** Per-agent namespaced (matches `protoagent.ui[:slug]` /
  `protoagent.chat.sessions[:slug]`), versioned with a migration that seeds the
  new `dockLayout` from today's `railOrder` + session order (no user-visible
  reset).
- **DS lift (the real cost on the component side).** The AppShell must expose a
  **dock mode** that hosts one `DndContext` across the rails *and* a panel's
  `TabBar`, replacing the two independent contexts (rails' `onRailReorder` +
  TabBar's `onReorder`). This is new protoContent DS work and is the gating
  dependency for the cross-container gestures (reorder-only works today without
  it).
- **Rules layer.** Forbidden/placement constraints as data keyed by view `kind`
  (e.g. `session` may not enter `bottom`), plus empty-container handling and
  active-tab fallback (all already in the POC).

## 5. Sequencing

1. **Tab reorder in the DS `TabBar`** — protoContent #264 (landing). Reorder
   within a strip; useful on its own, no model change.
2. **This ADR** — unified `View` + `dockLayout` bridge + session-as-View +
   persistence/migration (protoAgent). Makes tear-out/re-dock *expressible*.
3. **DS AppShell dock mode** — one shared `DndContext` across rails + tab strip
   (protoContent). The cross-container gesture.
4. **Wire protoAgent to dock mode** — placement rules, the always-mounted
   contract under moves, polish.

Steps 1 and 3 are DS (protoContent); 2 and 4 are protoAgent. Each step is
shippable; the UX is fully realized at 4.

## 6. Alternatives considered

- **Adopt a docking library** (Dockview / FlexLayout / rc-dock / golden-layout).
  Full split/float/dock semantics out of the box — but it **replaces the bespoke
  AppShell** and the investment in it (theming, rails, utility bar — ADR
  0044/0046/0048), is heavy, and fights our token system. Rejected for now; the
  AppShell is load-bearing and the gesture set we want is narrower than a full
  docking manager.
- **Keep two models, special-case tear-out** with a bespoke session↔surface
  converter. The POC showed this is the combinatorial, fragile path; the clean
  one is unification. Rejected.
- **Ship reorder only (no tear-out).** Rail reorder exists; tab reorder lands in
  #264. This is the legitimate **stopping point if step 2 isn't worth it** — the
  baseline against which the unification must justify itself.

## 7. Consequences

- **Pro:** one mental model; the validated gesture set; sessions become
  placeable (side-by-side multi-session — a genuine workflow unlock); plugin/ext
  surfaces and sessions become uniformly dockable.
- **Con:** touches core layout state + persistence (migration risk); requires a
  DS AppShell change (shared context); generalizing the chat slot contract needs
  care.
- **Risk:** persistence migration for existing users (`railOrder` + sessions);
  the always-mounted streaming contract under moves; plugin views assuming a
  fixed placement.

## 8. Open questions

- Does session-as-View hold the always-mounted chat contract (ADR 0045)? Define
  mount semantics for N concurrent session-views (and a cap).
- Multi-panel **splits** — v2, or pulled into v1? (v1 assumes rails + one main
  panel.)
- Bridge (B) vs consolidate (A) — and when to graduate.
- Placement rules as data per `kind` vs hardcoded — and who owns the rule set
  (core vs plugin-declared).
