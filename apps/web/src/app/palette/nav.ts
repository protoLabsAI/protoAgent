// Palette navigation — the ONE chokepoint every palette navigation funnels through.
//
// Split out of the old monolithic `usePaletteRegistry.ts` (ADR 0057) so the ranked root
// view, the adapter, and the desktop launcher can each import just what they need. The
// public entry points are re-exported verbatim from `../usePaletteRegistry`, so nothing
// that imports them had to change.
import { useUI } from "../../state/uiStore";
import { agentHref } from "../../lib/api";

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
  | { kind: "agent"; slug: string };

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
    case "agent":
      // Switch the console to another fleet agent (slug-routed, ADR 0042) — a full navigation,
      // since that agent's chat, threads, and API surface all key off the URL slug. This runs in
      // a real console window (the launcher forwards the intent to the main window), so
      // `window.location` targets the window the operator is actually looking at.
      window.location.href = agentHref(intent.slug);
      break;
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
