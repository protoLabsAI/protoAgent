// ADR 0057 — the command-palette adapter. Feeds the DS palette registry from the
// console's existing sources: every resolvable View becomes a "go to" command via
// `useUI().setSurface(id)`, plus deep-link actions into sub-tabbed surfaces.
//
// Step 3a (inline plugin views): a plugin view that opts in (`views[].palette:
// "inline"`) is registered as a DS `pluginView()` — its command morphs the palette
// body into the plugin's own iframe (themed/authed via the same handshake) instead
// of navigating to its rail. (Plugin-declared `commands:` + dispatch are step 3b.)
import type { ReactNode } from "react";
import { useEffect, useMemo } from "react";
import { createPaletteRegistry, pluginView } from "@protolabsai/ui/command-palette";
import type { Command, PaletteRegistry, PaletteSource, PaletteView } from "@protolabsai/ui/command-palette";
import { useUI } from "../state/uiStore";
import type { View, ViewKind } from "../lib/viewRegistry";

const SURFACES: PaletteSource = { id: "surfaces", label: "Surfaces" };
const ACTIONS: PaletteSource = { id: "actions", label: "Actions" };
const AGENTS: PaletteSource = { id: "agents", label: "Agents" };

/** Optional inline chat with the focused agent (ADR 0057). App builds the native chat
 *  PaletteView (it needs JSX + the focused agent name); the adapter registers it + a
 *  "Chat with <agent>" command that morphs into it. */
export type PaletteChatConfig = {
  name: string;
  icon?: ReactNode;
  view: PaletteView;
};

const GROUP: Record<ViewKind, string> = {
  surface: "Surfaces",
  session: "Sessions",
  plugin: "Plugins",
  ext: "Surfaces",
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
 *  Reads live state via the store's `getState()` so it isn't a render subscription. */
export function openView(id: string) {
  const ui = useUI.getState();
  if (ui.railOrder.right.includes(id)) {
    ui.setRightCollapsed(false);
    ui.setRightPanel(id);
  } else if (ui.railOrder.bottom.includes(id)) {
    ui.setBottomCollapsed(false);
    ui.setBottomPanel(id);
  } else {
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
  | { kind: "global"; section: "fleet" | "telemetry" | "commons" };

/** Apply an intent to THIS window's UI store. The default navigator, and what the main
 *  window calls when it receives a forwarded intent from the launcher. */
export function applyNavIntent(intent: NavIntent) {
  const ui = useUI.getState();
  switch (intent.kind) {
    case "view":
      openView(intent.id);
      break;
    case "plugins":
      ui.setSurface("plugins");
      ui.setPluginsTab(intent.tab);
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

/** Deep-links into sub-tabbed surfaces. The sub-tab ids are the uiStore union types
 *  (the source of truth), so these can't drift into a 404 section. */
function deepLinkCommands(): Command[] {
  // Each deep-link is expressed as a serializable NavIntent routed through `navigate()`,
  // so it works identically in the console window (apply locally) and the desktop
  // launcher (forward to the main window).
  const link = (id: string, label: string, keywords: string[], intent: NavIntent): Command => ({
    id,
    label,
    group: "Jump to",
    keywords,
    run: (c) => {
      navigate(intent);
      c.close();
    },
  });
  return [
    // (Inbox moved to a utility-bar widget; Schedule is a top-level rail surface that
    // auto-registers as a "go to" nav command — so no Activity deep-links here.)
    link("plug:market", "Plugins: Discover", ["plugins", "discover", "market", "directory", "browse"], {
      kind: "plugins",
      tab: "market",
    }),
    // Install-from-URL is the advanced action under Installed now (ADR 0059 D4) — land there.
    link("plug:download", "Plugins: Install from URL", ["plugins", "install", "url", "git"], {
      kind: "plugins",
      tab: "local",
    }),
    // Global settings (Fleet/Telemetry/Commons) is the header-drawer overlay now
    // (2026-06-18 IA pass) — open it deep-linked to the section.
    link("box:fleet", "Settings: Fleet", ["fleet", "agents", "box", "global"], { kind: "global", section: "fleet" }),
    link("box:telemetry", "Settings: Telemetry", ["telemetry", "metrics", "box", "global"], {
      kind: "global",
      section: "telemetry",
    }),
    link("box:commons", "Settings: Shared Skills", ["commons", "shared", "skills", "box", "global"], {
      kind: "global",
      section: "commons",
    }),
  ];
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

  // Signatures key the re-register effects on the *content* (the array identity
  // changes every render; the ids/urls don't).
  const navSig = views.map((v) => `${v.id} ${v.title}`).join("|");
  const inlineSig = inlineViews.map((v) => `${v.id} ${v.url} ${v.title}`).join("|");

  // Views: inline plugin morph targets + the chat view. (View order doesn't affect the
  // command-list order.)
  useEffect(() => {
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
    if (vs.length === 0) return;
    return registry.registerViews(vs);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inlineSig, chat, registry]);

  // Commands, registered TOGETHER in a fixed order so SURFACES stay at the TOP of the
  // list even when the nav set re-registers as plugins load (re-registering a command
  // group re-appends it to the end — so registering them separately would sink the nav
  // group below deep-links/chat). Order: surfaces → deep-links → chat.
  useEffect(() => {
    const nav: Command[] = views.map((v) => {
      const inline = inlineIds.has(v.id);
      return {
        id: `nav:${v.id}`,
        label: v.title,
        hint: inline ? "open here" : "go to",
        icon: v.icon,
        group: GROUP[v.kind],
        keywords: ["go", "open", v.kind],
        // Inline plugin views morph IN PLACE (also in the launcher window); a plain
        // surface navigates — routed through `navigate()` so the launcher can hand it
        // off to the main window instead of mutating its own (shell-less) store.
        run: inline
          ? (c) => c.enter(v.id)
          : (c) => {
              navigate({ kind: "view", id: v.id });
              c.close();
            },
      };
    });
    const offNav = registry.registerCommands(nav, { source: SURFACES });
    const offLinks = registry.registerCommands(deepLinkCommands(), { source: ACTIONS });
    const offChat = chat
      ? registry.registerCommands(
          [
            {
              id: "chat",
              label: `Chat with ${chat.name}`,
              hint: "ask the agent",
              icon: chat.icon,
              group: "Agents",
              keywords: ["chat", "ask", "talk", "agent"],
              run: (c) => c.enter("chat"),
            },
          ],
          { source: AGENTS },
        )
      : undefined;
    return () => {
      offNav();
      offLinks();
      offChat?.();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navSig, inlineSig, chat, registry]);

  return registry;
}
