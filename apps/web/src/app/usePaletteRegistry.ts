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

/** Deep-links into sub-tabbed surfaces. The sub-tab ids are the uiStore union types
 *  (the source of truth), so these can't drift into a 404 section. */
function deepLinkCommands(): Command[] {
  const ui = () => useUI.getState();
  const link = (id: string, label: string, keywords: string[], go: () => void): Command => ({
    id,
    label,
    group: "Jump to",
    keywords,
    run: (c) => {
      go();
      c.close();
    },
  });
  return [
    link("act:inbox", "Activity: Inbox", ["activity", "inbox"], () => {
      ui().setSurface("activity");
      ui().setActivityTab("inbox");
    }),
    // (Schedule is a top-level rail surface again — it auto-registers as a "go to"
    // nav command, so no Activity deep-link here.)
    link("plug:market", "Plugins: Discover", ["plugins", "discover", "market", "directory", "browse"], () => {
      ui().setSurface("plugins");
      ui().setPluginsTab("market");
    }),
    // Install-from-URL is the advanced action under Installed now (ADR 0059 D4) — land there.
    link("plug:download", "Plugins: Install from URL", ["plugins", "install", "url", "git"], () => {
      ui().setSurface("plugins");
      ui().setPluginsTab("local");
    }),
    // Box folded into Settings ▸ Global (ADR 0048 follow-up) — land on the section.
    link("box:fleet", "Settings: Fleet", ["fleet", "agents", "box"], () => {
      ui().setSurface("settings");
      ui().setSettingsScope("host");
      ui().setSettingsSection("fleet");
    }),
    link("box:telemetry", "Settings: Telemetry", ["telemetry", "metrics", "box"], () => {
      ui().setSurface("settings");
      ui().setSettingsScope("host");
      ui().setSettingsSection("telemetry");
    }),
    link("box:commons", "Settings: Commons", ["commons", "shared", "skills", "box"], () => {
      ui().setSurface("settings");
      ui().setSettingsScope("host");
      ui().setSettingsSection("commons");
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
        run: inline
          ? (c) => c.enter(v.id)
          : (c) => {
              openView(v.id);
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
