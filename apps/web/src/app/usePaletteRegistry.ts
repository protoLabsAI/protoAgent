// ADR 0057 — the command-palette adapter. Feeds the DS palette registry from the
// console's existing sources, organized to be command-driven rather than a flat list of
// places. The root view reads top-to-bottom as: Agents (chat) → Plugins (each plugin's
// views) → Commands. The built-in surfaces are NOT dumped at the root — an `Open…` command
// morphs into an `Open ▸` submorph (a self-contained command list) so you don't see every
// surface until you ask for one. Deep-link actions ride in the Commands group too.
//
// Inline plugin views (a plugin view that opts in via `views[].palette: "inline"`) are
// registered as DS `pluginView()`s — their command morphs the palette body into the
// plugin's own iframe (themed/authed via the handshake) instead of navigating to its rail.
import type { ReactNode } from "react";
import { useEffect, useMemo } from "react";
import { commandsView, createPaletteRegistry, pluginView } from "@protolabsai/ui/command-palette";
import type { Command, PaletteRegistry, PaletteView } from "@protolabsai/ui/command-palette";
import { useUI } from "../state/uiStore";
import type { View } from "../lib/viewRegistry";
import { registerPaletteCommand, registeredPaletteCommands } from "../ext/paletteRegistry";
import type { PaletteCommand } from "../ext/paletteRegistry";

/** Optional inline chat with the focused agent (ADR 0057). App builds the native chat
 *  PaletteView (it needs JSX + the focused agent name); the adapter registers it + a
 *  "Chat with <agent>" command that morphs into it. */
export type PaletteChatConfig = {
  name: string;
  icon?: ReactNode;
  view: PaletteView;
};

/** A plugin view opted into inline morphing (`views[].palette: "inline"`). Carries
 *  everything the DS `pluginView()` needs to mount + run the handshake. */
export type InlinePluginView = {
  /** `plugin:<id>:<view>` — matches the view's nav id, so the command can `enter()` it. */
  id: string;
  title: string;
  /** Slug-aware resolved page URL (`apiUrl(view.path)`). */
  url: string;
  icon?: ReactNode;
  /** The curated 6-key console theme (`consoleTheme()`), posted on init. */
  theme: Record<string, string>;
  /** Operator bearer (`authToken()`) for the page's same-origin authed calls. */
  token: string;
  sandbox?: string;
};

/** Open any view by id, routed to the dock it actually lives on (and uncollapsed).
 *  Reads live state via the store's `getState()` so it isn't a render subscription.
 *  A HIDDEN surface (railOrder.hidden — enabled but not shown) is un-hidden first: the
 *  palette is the restore point, so ⌘K → a hidden view's name brings it back onto a dock. */
export function openView(id: string) {
  const ui = useUI.getState();
  if ((ui.railOrder.hidden ?? []).includes(id)) ui.showSurface(id); // restore onto its dock, then route
  const ro = useUI.getState().railOrder; // re-read: showSurface mutated it
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
  | { kind: "global"; section?: string };

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
  }
}

let navigator: (intent: NavIntent) => void = applyNavIntent;

/** Override where palette navigation goes (the launcher forwards to the main window).
 *  Pass `null` to restore the default local apply. */
export function setPaletteNavigator(fn: ((intent: NavIntent) => void) | null) {
  navigator = fn ?? applyNavIntent;
}

/** The single entry point every nav command + deep-link runs through. */
function navigate(intent: NavIntent) {
  navigator(intent);
}

// Core deep-link palette commands — DOGFOODED through the public `registerPaletteCommand`
// seam (ADR 0061), so core uses the same path a fork does (no bypass). Each deep-link is a
// serializable NavIntent routed through `navigate()`, so it works identically in the console
// window (apply locally) and the desktop launcher (forward to the main window). The sub-tab
// ids are the uiStore union types (source of truth), so they can't drift into a 404.
// (Inbox moved to a utility-bar widget; Schedule is a top-level rail surface that
// auto-registers as a "go to" nav command — so no Activity deep-links here.)
const _link = (id: string, label: string, keywords: string[], intent: NavIntent) =>
  registerPaletteCommand({
    id,
    label,
    group: "Commands",
    keywords,
    run: (ctx) => {
      navigate(intent);
      ctx.close();
    },
  });
_link("plug:market", "Plugins: Discover", ["plugins", "discover", "market", "directory", "browse"], {
  kind: "plugins",
  tab: "market",
});
// Install-from-URL is the advanced action under Installed now (ADR 0059 D4) — land there.
_link("plug:download", "Plugins: Install from URL", ["plugins", "install", "url", "git"], {
  kind: "plugins",
  tab: "local",
});
// Settings is the consolidated dialog now (2026-06) — opened from the utility-bar pill,
// the drawer, or these ⌘K commands. A bare "Settings" command + Box-section deep-links.
_link("settings", "Settings", ["settings", "config", "preferences", "options"], { kind: "global" });
_link("box:fleet", "Settings: Fleet", ["fleet", "agents", "box"], { kind: "global", section: "fleet" });
_link("box:telemetry", "Settings: Telemetry", ["telemetry", "metrics", "box", "global"], {
  kind: "global",
  section: "telemetry",
});

/** Map a registered (core or fork) PaletteCommand onto a DS palette `Command`. */
function toDsCommand(pc: PaletteCommand): Command {
  return {
    id: pc.id,
    label: pc.label,
    group: pc.group ?? "Commands",
    keywords: pc.keywords ?? [],
    run: (c) => pc.run({ close: () => c.close() }),
  };
}

/** Build the palette registry from the resolved view list + the inline plugin views.
 *  Stable across renders; nav commands + inline views re-register only when their set
 *  changes (plugins enable/disable) — matching the DS registry's add/withdraw model. */
export function usePaletteRegistry(
  views: View[],
  inlineViews: InlinePluginView[] = [],
  chat?: PaletteChatConfig,
): PaletteRegistry {
  const registry = useMemo(() => createPaletteRegistry(), []);
  const inlineIds = useMemo(() => new Set(inlineViews.map((v) => v.id)), [inlineViews]);

  // Built-in surfaces (core + fork/ext) live behind `Open ▸`; plugin views are their own
  // root section. A `session` view (none today) would ride with the built-ins.
  const surfaceViews = views.filter((v) => v.kind !== "plugin");
  const pluginViewsList = views.filter((v) => v.kind === "plugin");

  // Signatures key the re-register effects on the *content* (the array identity
  // changes every render; the ids/urls don't).
  const navSig = views.map((v) => `${v.id} ${v.title}`).join("|");
  const inlineSig = inlineViews.map((v) => `${v.id} ${v.url} ${v.title}`).join("|");

  // Views the palette can morph into: inline plugin iframes, the chat view, and the
  // `Open ▸` submorph (a self-contained command list of the built-in surfaces, so the root
  // stays a short command list — you don't see every surface until you enter Open).
  useEffect(() => {
    const openSurfaceCommands: Command[] = surfaceViews.map((v) => ({
      id: `open:${v.id}`,
      label: v.title,
      icon: v.icon,
      keywords: ["open", "go", "surface", v.kind],
      run: (c) => {
        navigate({ kind: "view", id: v.id });
        c.close();
      },
    }));
    const vs: PaletteView[] = inlineViews.map((v) =>
      pluginView({
        id: v.id,
        title: v.title,
        url: v.url,
        theme: v.theme,
        token: v.token,
        sandbox: v.sandbox,
        height: 460,
      }),
    );
    if (chat) vs.push(chat.view);
    vs.push({
      ...commandsView({ commands: openSurfaceCommands, placeholder: "Open a surface…" }),
      id: "open",
      title: "Open",
    });
    return registry.registerViews(vs);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navSig, inlineSig, chat, registry]);

  // Root commands, registered in DISPLAY order (the list renders groups in registration
  // order). Agents → Plugins → Commands. Re-registered atomically when the view set
  // changes (plugins load), so the order never drifts.
  useEffect(() => {
    const offChat = chat
      ? registry.registerCommands([
          {
            id: "chat",
            label: `Chat with ${chat.name}`,
            hint: "ask the agent",
            icon: chat.icon,
            group: "Agents",
            keywords: ["chat", "ask", "talk", "agent"],
            run: (c) => c.enter("chat"),
          },
        ])
      : undefined;
    // Each plugin's views: inline ones morph IN PLACE (also in the launcher window); a
    // rail view navigates — routed through `navigate()` so the launcher hands it off to
    // the main window instead of mutating its own (shell-less) store.
    const pluginCommands: Command[] = pluginViewsList.map((v) => {
      const inline = inlineIds.has(v.id);
      return {
        id: `nav:${v.id}`,
        label: v.title,
        // No "open" verb/keyword here — `Open…` is its own command now and would collide.
        // An inline plugin morphs in place (no hint); a rail view navigates ("go to").
        hint: inline ? undefined : "go to",
        icon: v.icon,
        group: "Plugins",
        keywords: ["plugin", v.kind],
        run: inline
          ? (c) => c.enter(v.id)
          : (c) => {
              navigate({ kind: "view", id: v.id });
              c.close();
            },
      };
    });
    const offPlugins = pluginCommands.length ? registry.registerCommands(pluginCommands) : undefined;
    // Commands group: `Open ▸` (morphs to the built-in surfaces) + the deep-link actions.
    const openCommand: Command = {
      id: "open",
      label: "Open…",
      hint: "surface",
      group: "Commands",
      keywords: ["open", "go to", "surface", "view", "navigate", "switch", "panel"],
      run: (c) => c.enter("open"),
    };
    const offCommands = registry.registerCommands([openCommand, ...registeredPaletteCommands().map(toDsCommand)]);
    return () => {
      offChat?.();
      offPlugins?.();
      offCommands();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navSig, inlineSig, chat, registry]);

  return registry;
}
