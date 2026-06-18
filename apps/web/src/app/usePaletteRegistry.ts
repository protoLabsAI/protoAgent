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
import { chatView, createPaletteRegistry, pluginView } from "@protolabsai/ui/command-palette";
import type { AgentTransport, Command, PaletteRegistry, PaletteSource } from "@protolabsai/ui/command-palette";
import { useUI } from "../state/uiStore";
import type { View, ViewKind } from "../lib/viewRegistry";

const SURFACES: PaletteSource = { id: "surfaces", label: "Surfaces" };
const ACTIONS: PaletteSource = { id: "actions", label: "Actions" };
const AGENTS: PaletteSource = { id: "agents", label: "Agents" };

/** Optional inline chat with the focused agent (ADR 0057). App builds the transport
 *  (it owns the agent name + the icon node); the adapter registers the view + command. */
export type PaletteChat = {
  name: string;
  transport: AgentTransport;
  icon?: ReactNode;
  greeting?: ReactNode;
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
    link("act:schedule", "Activity: Schedule", ["activity", "schedule", "cron"], () => {
      ui().setSurface("activity");
      ui().setActivityTab("schedule");
    }),
    link("plug:market", "Plugins: Discover", ["plugins", "discover", "market", "directory", "browse"], () => {
      ui().setSurface("plugins");
      ui().setPluginsTab("market");
    }),
    // Install-from-URL is the advanced action under Installed now (ADR 0059 D4) — land there.
    link("plug:download", "Plugins: Install from URL", ["plugins", "install", "url", "git"], () => {
      ui().setSurface("plugins");
      ui().setPluginsTab("local");
    }),
    link("box:telemetry", "Box: Telemetry", ["box", "telemetry", "metrics"], () => {
      ui().setSurface("box");
      ui().setBoxTab("telemetry");
    }),
    link("box:commons", "Box: Commons", ["box", "commons", "shared"], () => {
      ui().setSurface("box");
      ui().setBoxTab("commons");
    }),
  ];
}

/** Build the palette registry from the resolved view list + the inline plugin views.
 *  Stable across renders; nav commands + inline views re-register only when their set
 *  changes (plugins enable/disable) — matching the DS registry's add/withdraw model. */
export function usePaletteRegistry(
  views: View[],
  inlineViews: InlinePluginView[] = [],
  chat?: PaletteChat,
): PaletteRegistry {
  const registry = useMemo(() => createPaletteRegistry(), []);
  const inlineIds = useMemo(() => new Set(inlineViews.map((v) => v.id)), [inlineViews]);

  // Signatures key the re-register effects on the *content* (the array identity
  // changes every render; the ids/urls don't).
  const navSig = views.map((v) => `${v.id} ${v.title}`).join("|");
  const inlineSig = inlineViews.map((v) => `${v.id} ${v.url} ${v.title}`).join("|");

  // Register inline plugin views as DS pluginViews (the morph targets).
  useEffect(() => {
    if (inlineViews.length === 0) return;
    return registry.registerViews(
      inlineViews.map((v) =>
        pluginView({
          id: v.id,
          title: v.title,
          url: v.url,
          theme: v.theme,
          token: v.token,
          sandbox: v.sandbox,
          height: 460,
        }),
      ),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inlineSig, registry]);

  // Nav commands — an inline view's command MORPHS the palette into its iframe
  // (`enter`, stays open); everything else navigates and closes.
  useEffect(() => {
    const cmds: Command[] = views.map((v) => {
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
    return registry.registerCommands(cmds, { source: SURFACES });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navSig, inlineSig, registry]);

  useEffect(() => registry.registerCommands(deepLinkCommands(), { source: ACTIONS }), [registry]);

  // Inline chat with the focused agent — ⌘K → morph the palette into a chat that
  // streams turns via api.streamChat (an ephemeral context per open; see paletteChat).
  // The DS chat view focuses its composer on open, so it's type-ready immediately.
  useEffect(() => {
    if (!chat) return;
    const offView = registry.registerViews([chatView({ title: chat.name })]);
    const offCmd = registry.registerCommands(
      [
        {
          id: "chat",
          label: `Chat with ${chat.name}`,
          hint: "ask the agent",
          icon: chat.icon,
          group: "Agents",
          keywords: ["chat", "ask", "talk", "agent"],
          run: (c) => c.enter("chat", { transport: chat.transport, greeting: chat.greeting }),
        },
      ],
      { source: AGENTS },
    );
    return () => {
      offView();
      offCmd();
    };
  }, [registry, chat]);

  return registry;
}
