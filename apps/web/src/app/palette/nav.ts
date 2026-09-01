// Palette navigation — the ONE chokepoint every palette navigation funnels through.
//
// Split out of the old monolithic `usePaletteRegistry.ts` (ADR 0057) so the ranked root
// view, the adapter, and the desktop launcher can each import just what they need. The
// public entry points are re-exported verbatim from `../usePaletteRegistry`, so nothing
// that imports them had to change.
import { flushSync } from "react-dom";

import { useUI } from "../../state/uiStore";
import { chatStore } from "../../chat/chat-store";
import { agentHref } from "../../lib/api";
import { seedKnowledgeSearch } from "../../knowledge/searchSeed";
import { runBindingById } from "../../keybindings/useKeybindings";

/** Open any view by id, routed to the dock it actually lives on (and uncollapsed).
 *  Reads live state via the store's `getState()` so it isn't a render subscription.
 *  A HIDDEN surface (railOrder.hidden — enabled but not shown) is un-hidden first: the
 *  palette is the restore point, so ⌘⇧K → a hidden view's name brings it back onto a dock. */
export function openView(id: string) {
  const ui = useUI.getState();
  if ((ui.railOrder.hidden ?? []).includes(id)) ui.showSurface(id); // restore onto its dock, then route
  const ro = useUI.getState().railOrder; // re-read: showSurface mutated it
  // The mobile shell reads `mobileActive`, NOT the per-dock ids — so without this every
  // programmatic navigation (a palette "go to", the rail context menu, a plugin's `ui.navigate`,
  // a launcher intent) silently did nothing on a phone: it moved a dock the mobile shell
  // never renders. Set both; `mobileActive` is inert on desktop.
  ui.setMobileActive(id);
  if (ro.right.includes(id)) {
    ui.setRightCollapsed(false);
    ui.setRightPanel(id);
  } else if (ro.bottom.includes(id)) {
    ui.setBottomCollapsed(false);
    ui.setBottomPanel(id);
  } else {
    ui.setLeftCollapsed(false);
    ui.setSurface(id);
  }
}

// ── Navigation handoff (desktop launcher, ADR 0057) ────────────────────────────────
// Every palette navigation funnels through `navigate(intent)` so it has ONE chokepoint.
// In the normal console window the intent applies to THIS window's store (the default).
// In the frameless desktop launcher window the store is a separate JS context with no
// shell — so the launcher swaps the sink (`setPaletteNavigator`) to forward the intent
// to the main window over a Tauri event, which replays it there via `applyNavIntent`.

/** A serializable description of "where the palette wants to go" — so it can cross the
 *  window boundary as a plain event payload. */
export type NavIntent =
  | { kind: "view"; id: string }
  | { kind: "plugins"; tab: "local" | "market" }
  | { kind: "global"; section?: string }
  | { kind: "agent"; slug: string }
  // Open the Knowledge surface ON A SEARCH TERM (the ⌘K knowledge rows). A separate arm
  // rather than `{ kind: "view", id: "knowledge" }` because the term is the point: the
  // surface has no per-chunk anchor, so re-running the operator's own search is what puts
  // the row they picked in front of them. Stays a plain serializable payload, so it crosses
  // the launcher → main-window event boundary like every other intent.
  // `query` is REQUIRED, so this arm has exactly one meaning. A query-less "just open
  // Knowledge" is already `{ kind: "view", id: "knowledge" }`, and letting it in here would
  // add a second reading — does an absent term leave the surface alone, or clear it? — for
  // a caller that has no reason to exist.
  | { kind: "knowledge"; query: string }
  // Run a registered keybinding's action (ADR 0063) — what a ⌘K row that advertises a
  // shortcut does. An INTENT rather than a direct `binding.run()` at the row for the usual
  // launcher reason: these actions mutate the console's stores and walk its DOM, both of
  // which are absent in the frameless launcher window, so the work has to be able to cross
  // to the main window as a plain payload. `surface` is the view that makes a SCOPED
  // binding's scope real — see the case in `applyNavIntent`.
  | { kind: "keybinding"; id: string; surface?: string }
  // Switch the chat tab strip to one already-open session (`app/chatTabPalette.ts`). An
  // INTENT rather than a direct `chatStore.switchSession` at the call site so it crosses the
  // launcher boundary like every other navigation, and so the id is validated in the window
  // that owns the store — see `applyNavIntent`.
  | { kind: "chat"; sessionId: string };

/** Apply an intent to THIS window's UI store. The default navigator, and what the main
 *  window calls when it receives a forwarded intent from the launcher. */
export function applyNavIntent(intent: NavIntent) {
  const ui = useUI.getState();
  switch (intent.kind) {
    case "view":
      openView(intent.id);
      break;
    case "plugins":
      // Plugins is a Settings section; Settings is the dialog now (2026-06). Open it on the
      // Plugins section with the right inner tab (Installed/Discover).
      ui.setPluginsTab(intent.tab);
      ui.openGlobalSettings("plugins");
      break;
    case "global":
      ui.openGlobalSettings(intent.section);
      break;
    case "knowledge":
      // Seed BEFORE routing: the surface is usually not mounted yet, and adopting the term
      // in its first effect is what keeps the landing paint from flashing the unrelated
      // recent-chunks listing. Already-open is covered too — the seed store is subscribed.
      seedKnowledgeSearch(intent.query);
      openView("knowledge");
      break;
    case "agent":
      // Switch the console to another fleet agent (slug-routed, ADR 0042) — a full navigation,
      // since that agent's chat, threads, and API surface all key off the URL slug. This runs in
      // a real console window (the launcher forwards the intent to the main window), so
      // `window.location` targets the window the operator is actually looking at.
      window.location.href = agentHref(intent.slug);
      break;
    case "keybinding": {
      // Make the binding's SCOPE real before running it, rather than bypassing it.
      // `resolveBinding` is the only place `scope` is enforced and a palette row calls
      // `run()` directly, so a chat-scoped action would otherwise fire from an overlay that
      // is never inside `[data-kb-scope="chat"]`. Navigating first is also what the operator
      // asked for — "Clear conversation" chosen from Knowledge means "go to chat and clear
      // it". A row for a scope with no surface is never built (keybindingCommands.ts).
      //
      // …and `flushSync`, because "navigating first" has to mean the DOM, not the store.
      // `openView` only writes zustand state; the row runs from a React event handler, so
      // React commits AFTER the handler returns — the next line would otherwise read a DOM
      // the navigation had not produced yet. That matters because the chat slot's #613
      // "mounted for the app's LIFETIME" contract is about the slot within its DOCK, and the
      // DS AppShell renders each column conditionally (`const showLeft = !leftCollapsed`;
      // `{showLeft && <main className="pl-appshell__col--left">…</main>}`, app-shell.tsx). A
      // collapsed dock takes `.chat-session-slot` and `[data-kb-scope="chat"]` out of the
      // document entirely, so `chat.tool.toggle` — the one action here that WALKS the DOM
      // (toolCollapse.ts `chatRoot()`) — found nothing and returned silently, re-opening the
      // panel and doing nothing else. Flushing keeps every store-only action in this switch
      // synchronous and observable (a deferred run would make `chat.clear` / `chat.new`
      // land a frame later, in the launcher hand-off too), while giving the DOM-walking one
      // a committed tree. Cheap: one navigation, only when a row names a surface.
      // (Braced: `const` in a bare case clause leaks into the whole switch.)
      const surface = intent.surface;
      if (surface) flushSync(() => openView(surface));
      runBindingById(intent.id);
      break;
    }
    case "chat": {
      // Re-validate against a FRESH snapshot before switching. A palette row is built when
      // the tab strip last changed and run a keystroke later, and `chatStore.switchSession`
      // does not check the id it is handed: for a session deleted in that window (another
      // browser tab's cross-tab merge, a background job) it still sets `currentSessionId` and
      // pushes the phantom id into `activeSessions` — evicting a real tab — after which
      // ChatSurface's `useSession` finds nothing and mounts a DEAD SLOT.
      //
      // The check belongs HERE and not in the row's `run()`: in the desktop launcher `run()`
      // executes in a different JS context from the store that is about to be mutated, so a
      // check there would validate against the wrong snapshot. This is the window that owns it.
      // Not a theoretical mismatch: chat sessions are keyed PER AGENT in localStorage
      // (chat-store.ts, ADR 0042 slug routing) and the launcher always loads the un-slugged
      // app URL, so while the main window sits on `/app/agent/<slug>/` the launcher is
      // listing the HOST's chats and forwarding ids that window has never seen. They land
      // here, on the chat surface, rather than on a dead slot.
      //
      // A vanished chat still routes to the chat surface — the operator lands where their
      // chats are and can see the tab is gone, which beats a click that does nothing at all.
      if (chatStore.getSnapshot().sessions.some((s) => s.id === intent.sessionId)) {
        chatStore.switchSession(intent.sessionId);
      }
      openView("chat");
      break;
    }
  }
}

let navigator: (intent: NavIntent) => void = applyNavIntent;

/** Override where palette navigation goes (the launcher forwards to the main window).
 *  Pass `null` to restore the default local apply. */
export function setPaletteNavigator(fn: ((intent: NavIntent) => void) | null) {
  navigator = fn ?? applyNavIntent;
}

/** The single entry point every nav command + deep-link runs through. */
export function navigate(intent: NavIntent) {
  navigator(intent);
}
