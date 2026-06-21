import {
  Activity,
  BarChart3,
  BookMarked,
  Database,
  BookOpen,
  Boxes,
  CalendarClock,
  CircleAlert,
  FileText,
  Gauge,
  Inbox,
  LayoutDashboard,
  Loader2,
  MessageSquare,
  PanelBottom,
  PanelLeft,
  PanelRight,
  Plus,
  Puzzle,
  Save,
  Settings2,
  Sparkles,
  Target,
  Undo2,
  Trash2,
  Wrench,
  // Plugin-view rail icons (ADR 0026) — a broader lucide allowlist so plugins
  // (dashboards, data, comms, dev, finance, space/fleet, AI) find a fitting glyph.
  Bot,
  Brain,
  Code,
  Coins,
  Compass,
  Cpu,
  DollarSign,
  Folder,
  GitBranch,
  Globe,
  Layers,
  LineChart,
  Map,
  Network,
  Package,
  PieChart,
  Plug,
  Radar,
  Rocket,
  Satellite,
  Shield,
  Ship,
  Table,
  Terminal,
  TrendingUp,
  Wallet,
  Workflow,
  Zap,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { ComponentType, LazyExoticComponent, ReactNode } from "react";
import { FleetTurnWatch } from "./FleetTurnWatch";
import { UpdateNotice } from "./UpdateNotice";
import { BackgroundWatch } from "./BackgroundWatch";
import { ChatResumeWatch } from "./ChatResumeWatch";
import { BackgroundJobs } from "./BackgroundJobs";
import { ProtoLabsIcon } from "./ProtoLabsIcon";
import { AuthGate } from "./AuthGate";
import { authRequired, subscribeAuth } from "../lib/auth";
import { TenantGuard } from "./TenantGuard";
import { Splash, BootGate } from "@protolabsai/ui/splash";
import { Button } from "@protolabsai/ui/primitives";

import { ActivityWidget } from "../activity/ActivityWidget";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { InboxWidget } from "../inbox/InboxWidget";
import { ChatSlot } from "./ChatSlot";
import { useAnyChatStreaming } from "../chat/chat-store";
import { KnowledgeStore } from "../knowledge/KnowledgeStore";
import { SettingsOverlay } from "../settings/SettingsOverlay";
import { AppDrawer } from "./AppDrawer";
import { HamburgerMenu } from "./HamburgerMenu";
import { FleetSwitcher } from "./FleetSwitcher";
import {
  useUI,
  type RightPanel,
  type Surface,
} from "../state/uiStore";
import { api, apiUrl, authToken, is401 } from "../lib/api";
import { PluginView, consoleTheme } from "./PluginView";
import { UtilityWidget } from "./UtilityWidget";
import { AppShell, Header, UtilityBar } from "@protolabsai/ui/app-shell";
import { CommandPalette, usePaletteHotkey } from "@protolabsai/ui/command-palette";
import type { PaletteView } from "@protolabsai/ui/command-palette";
import { Alert } from "@protolabsai/ui/data";
import { useIsMobile } from "../lib/useIsMobile";
import { useActiveTheme } from "../lib/useActiveTheme";
import { registeredSurfaces } from "../ext"; // build-time fork seam (ADR 0038 D3); also self-loads fork surfaces
import { ContextMenuRenderer, openContextMenu } from "../contextMenu";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { brandName } from "../lib/brand";
import { onConnectionChange, onServerEvent, onTopic } from "../lib/events";
import { useToast } from "@protolabsai/ui/overlays";
import { StatusPill } from "./StatusPill";
import { WorkPanel } from "./WorkPanel";
import { SetupWizard } from "../setup/SetupWizard";
import { hostRuntimeStatusQuery, runtimeStatusQuery } from "../lib/queries";
import { buildViews } from "../lib/viewRegistry";
import { applyNavIntent, usePaletteRegistry } from "./usePaletteRegistry";
import type { NavIntent } from "./usePaletteRegistry";
import { PaletteChat } from "./PaletteChat";
import { CORE_SURFACES } from "./coreSurfaces";
import { listen } from "../lib/desktop";

// Consolidated nav (heavy grouping): four rail surfaces, each grouped one
// fanning out to sub-views via an in-surface segmented control.
// Core surfaces are the fixed literals; plugin views (ADR 0026) add dynamic
// surfaces keyed `plugin:<pluginId>:<viewId>`. The `(string & {})` keeps literal
// autocomplete while allowing those runtime keys.

// A plugin view names its rail glyph by lucide icon name. The curated set below
// is the common-case fast path (already bundled); anything else falls back to the
// full lucide set by name, so a plugin author can use ANY lucide icon — PascalCase
// (`LineChart`) or kebab-case (`line-chart`) — without us extending an allowlist.
// Unknown/missing → a generic plugin glyph.
const PLUGIN_VIEW_ICONS: Record<string, LucideIcon> = {
  // general
  Sparkles, LayoutDashboard, Puzzle, Boxes, Gauge, Target, Activity, Settings2,
  // data / viz
  BarChart3, LineChart, PieChart, TrendingUp, Database, Table, Layers,
  // comms / content
  MessageSquare, Inbox, CalendarClock, FileText, Folder, BookOpen, BookMarked,
  // dev / tools
  Code, Terminal, GitBranch, Package, Plug, Workflow, Network, Cpu, Zap,
  // ai
  Bot, Brain,
  // finance
  DollarSign, Coins, Wallet,
  // space / fleet / geo
  Rocket, Ship, Satellite, Radar, Globe, Compass, Map,
  // security
  Shield,
};
// "line-chart" / "line_chart" / "LineChart" → "LineChart" (lucide's key style).
function toPascalCase(name: string): string {
  return name.replace(/(^|[-_ ])([a-z0-9])/g, (_m, _sep, ch: string) => ch.toUpperCase());
}

// Off the curated path, resolve ANY lucide icon by name — but lazily: the dynamic
// import pulls the full lucide set into a separate chunk that only loads when a
// plugin actually uses a non-curated glyph, so the main bundle stays lean.
type IconComp = LazyExoticComponent<ComponentType<{ size?: number }>>;
// NB: `Map` is shadowed by the lucide Map icon import — use the global explicitly.
const lazyIconCache = new globalThis.Map<string, IconComp>();
function lazyLucideIcon(key: string): IconComp {
  let comp = lazyIconCache.get(key);
  if (!comp) {
    comp = lazy(async () => {
      const m = await import("lucide-react");
      const Icon = (m.icons as Record<string, LucideIcon>)[key] || m.Puzzle;
      return { default: Icon as ComponentType<{ size?: number }> };
    });
    lazyIconCache.set(key, comp);
  }
  return comp;
}
function pluginViewIcon(name?: string, size = 18): ReactNode {
  if (!name) return <Puzzle size={size} />;
  const Curated = PLUGIN_VIEW_ICONS[name];
  if (Curated) return <Curated size={size} />;
  const Lazy = lazyLucideIcon(toPascalCase(name));
  return (
    <Suspense fallback={<Puzzle size={size} />}>
      <Lazy size={size} />
    </Suspense>
  );
}
// Studio = the workflow authoring/inspection surface. Per ADR 0020 execution is
// a chat gesture (run subagents/workflows via /<name>), not a surface — so the
// old "Run" tab is gone and Studio is just Workflows.
// Agent = the agent's own makeup: its identity (name + SOUL.md), tools, MCP
// servers, subagents, skills, and middleware. (Runtime status + telemetry moved
// to Settings → Overview.)
// Plugins = installed (local), discover (market), and install-from-git (download).
// Knowledge = the store + its settings (memory/knowledge config).
// Activity = the "triggers / events" surface (ADR 0009): what happened (thread),
// inbound (inbox), and timed (schedule — cron is a trigger, not a work-type).
// The agent's persistent working memory, grouped in the right sidebar:
// its notebook, its task board, and its goals.

function useLocalStorageState(key: string, fallback: string) {
  const [value, setValue] = useState(() => {
    try {
      return window.localStorage.getItem(key) || fallback;
    } catch {
      return fallback;
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      // localStorage can be unavailable in hardened browser contexts.
    }
  }, [key, value]);

  return [value, setValue] as const;
}

export function App() {
  // Navigation/layout state lives in the persisted UI store (ADR 0035 D5) — a refresh
  // restores the active surface, sub-tabs, and right-panel width/collapse.
  const surface = useUI((s) => s.surface);
  const setSurface = useUI((s) => s.setSurface);
  // Background-streaming indicator for the Chat rail (narrow selector → only
  // re-renders when the boolean flips, not per token).
  const chatStreaming = useAnyChatStreaming();
  const setFleetStartNew = useUI((s) => s.setFleetStartNew);
  const rightPanel = useUI((s) => s.rightPanel);
  const setRightPanel = useUI((s) => s.setRightPanel);
  const rightCollapsed = useUI((s) => s.rightCollapsed);
  const setRightCollapsed = useUI((s) => s.setRightCollapsed);
  const leftCollapsed = useUI((s) => s.leftCollapsed);
  const setLeftCollapsed = useUI((s) => s.setLeftCollapsed);
  const rightWidth = useUI((s) => s.rightWidth);
  const setRightWidth = useUI((s) => s.setRightWidth);
  const bottomPanel = useUI((s) => s.bottomPanel);
  const setBottomPanel = useUI((s) => s.setBottomPanel);
  const bottomHeight = useUI((s) => s.bottomHeight);
  const setBottomHeight = useUI((s) => s.setBottomHeight);
  const bottomCollapsed = useUI((s) => s.bottomCollapsed);
  const setBottomCollapsed = useUI((s) => s.setBottomCollapsed);
  const railOrder = useUI((s) => s.railOrder);
  const reconcilePluginViews = useUI((s) => s.reconcilePluginViews);
  const reconcileCoreSurfaces = useUI((s) => s.reconcileCoreSurfaces);
  const isMobile = useIsMobile();
  useActiveTheme(); // apply the focused agent's saved theme on boot + repaint on switch (ADR 0042)
  const mobileActive = useUI((s) => s.mobileActive);
  const setMobileActive = useUI((s) => s.setMobileActive);
  const quickBar = useUI((s) => s.quickBar);
  const setRailOrder = useUI((s) => s.setRailOrder);
  const [live, setLive] = useState(false);
  // Shared custom confirm for destructive actions (notes/tasks delete).
  const [confirmState, setConfirmState] = useState<
    null | { title: string; message?: string; confirmLabel?: string; onConfirm: () => void }
  >(null);
  // Global settings overlay (the Global home) — store-driven so BOTH the header drawer
  // and command-palette deep-links (Fleet/Telemetry/Commons) can open it. The header
  // hamburger's app drawer itself is local (only App triggers it).
  const globalSettingsOpen = useUI((s) => s.globalSettingsOpen);
  const globalSettingsSection = useUI((s) => s.globalSettingsSection);
  const openGlobalSettings = useUI((s) => s.openGlobalSettings);
  const closeGlobalSettings = useUI((s) => s.closeGlobalSettings);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [projectPath, setProjectPath] = useLocalStorageState("protoagent.projectPath", "");
  // Shell-level runtime read (ADR 0013): non-suspense useQuery so the topbar
  // always renders; the retry doubles as the desktop sidecar boot-probe. The
  // System → Runtime panel reads the same key via useSuspenseQuery. Keep polling
  // until the graph is compiled (`graph_loaded`) so the BootGate observes the
  // engine coming up — the post-setup compile runs inline on the server loop and
  // briefly freezes it, so we want to notice the moment it's live again.
  const runtimeQ = useQuery({
    ...runtimeStatusQuery(),
    // 30 boot-probe retries, but a 401 stops immediately: retrying can't fix a
    // missing bearer, and a retrying probe would reopen the AuthGate the moment
    // the operator dismissed it (#873). The gate's invalidateQueries restarts
    // the probe once a token is saved.
    retry: (failureCount, error) => !is401(error) && failureCount < 30,
    retryDelay: 1000,
    refetchInterval: (q) => (q.state.data?.graph_loaded ? false : 2500),
  });
  const runtime = runtimeQ.data ?? null;

  // Tenant uid is the HUB's, never the focused agent's (which changes on every fleet
  // swap and would wrongly wipe the chat view). Host-pinned, stable, low-churn.
  const hostUidQ = useQuery({
    ...hostRuntimeStatusQuery(),
    retry: (failureCount, error) => !is401(error) && failureCount < 30,
    retryDelay: 1000,
  });

  // Plugin-contributed rail surfaces (ADR 0026): each enabled plugin's declared
  // views become a dynamic rail icon (keyed plugin:<id>:<viewId>) whose panel is
  // an iframe of the page the plugin serves. PR1 thin vertical — PR2 generalizes
  // the rail into a full registry.
  // A plugin view declares its placement: "rail" (default — a left-rail surface) or
  // "right" (a right-sidebar panel, alongside Notes/Tasks/Goals/Schedule — ADR 0026).
  const allDeclaredViews = (runtime?.plugins ?? [])
    .filter((p) => p.enabled && p.views?.length)
    .flatMap((p) =>
      (p.views ?? []).map((v) => ({
        ...v,
        key: `plugin:${p.id}:${v.id}`,
        // Carry the owning plugin's load state so PluginView can phrase a real error
        // (enabled-but-not-loaded ⇒ missing env / bad deps / mount race; `error` is the
        // loader's exact diagnostic) instead of a blank "no details" panel.
        pluginLoaded: p.loaded,
        pluginError: p.error,
      })),
    );
  // A view claiming the chat SLOT (ADR 0045) replaces the built-in chat panel — it
  // renders under the core "chat" rail id, so it's excluded from the dynamic rail
  // list below. First enabled claimant wins (deterministic: plugin load order).
  const chatSlotView = allDeclaredViews.find((v) => v.slot === "chat");
  // A view with `utility` is a bottom-left utility-bar widget (a pill → dialog), NOT a rail
  // surface — so it's excluded from the rail list, like the chat-slot claimant.
  const utilityWidgetViews = allDeclaredViews.filter((v) => v.utility);
  const allPluginViews = allDeclaredViews.filter((v) => v.slot !== "chat" && !v.utility);
  // Enabled plugin ids — gates ext surfaces that declare requiresPlugin (e.g. Studio → workflows).
  const enabledPluginIds = new Set((runtime?.plugins ?? []).filter((p) => p.enabled).map((p) => p.id));

  // Plugin-driven console navigation (ADR 0044) — any plugin can ask to open one of its
  // own views via the reserved `ui.navigate` host intent `{plugin, view}` (emitted by
  // `registry.navigate(view)`). Core honors it GENERICALLY: resolve `plugin:<plugin>:<view>`
  // (blank view → that plugin's first view) and focus it only if the surface exists. No
  // per-plugin code — this is the single extensibility point for "the AI navigates the UI".
  const pluginViewsRef = useRef(allPluginViews);
  pluginViewsRef.current = allPluginViews;
  useEffect(
    () =>
      onServerEvent("ui.navigate", (d) => {
        const pid = String(d?.plugin || "");
        if (!pid) return;
        const own = pluginViewsRef.current.filter((v) => v.key.startsWith(`plugin:${pid}:`));
        const target = d?.view ? own.find((v) => v.key === `plugin:${pid}:${d.view}`) : own[0];
        if (target) setSurface(target.key);
      }),
    [],
  );

  const pluginRail = allPluginViews.filter((v) => (v.placement ?? "rail") !== "right" && v.placement !== "bottom");
  const pluginRightPanels = allPluginViews.filter((v) => v.placement === "right");
  const pluginBottom = allPluginViews.filter((v) => v.placement === "bottom");
  const activePluginView = pluginRail.find((v) => v.key === surface) ?? null;

  // Stale-surface fallback: if we're on a plugin view that no longer exists (its
  // plugin was disabled/removed, or a config reload dropped it) — once runtime is
  // loaded so we don't bounce during boot — fall back to chat instead of a blank
  // stage. (ADR 0026.)
  useEffect(() => {
    if (runtime && typeof surface === "string" && surface.startsWith("plugin:") && !activePluginView) {
      setSurface("chat");
    }
  }, [runtime, surface, activePluginView]);
  // White-label the window/tab title to the configured identity (default
  // protoAgent), so a fork's title follows its name without a rebuild.
  // brandName() display-cases a bare lower-case slug (e.g. `gina` → `Gina`).
  useEffect(() => {
    document.title = brandName(runtime?.identity?.name);
  }, [runtime]);
  // BootGate gating: show the app once the engine is ready (graph compiled) OR
  // the setup wizard is due (no graph expected pre-setup). `bootOverride` is the
  // manual escape hatch (BootGate's "Continue anyway") for a graph that never
  // compiles. The graph-ready transition also clears the stale connection-error
  // strip left behind by the compile-window freeze (see effect below).
  const [bootOverride, setBootOverride] = useState(false);
  const setupPending = Boolean(runtime) && runtime?.setup_complete === false;
  const engineReady = Boolean(runtime?.graph_loaded);
  const bootReady = bootOverride || setupPending || engineReady;
  // The boot gate state the DS BootGate (a slot-only shell) doesn't own: whether
  // the runtime probe has given up (`bootFailed`), and the post-grace "stuck"
  // copy/escape-hatch swap. STUCK_AFTER_MS=45s — past it, offer "Continue anyway"
  // so a graph that never compiles can't trap the operator on the loading screen.
  const bootFailed = !runtime && runtimeQ.isError;
  // Token-gated first run (#873): the boot probe itself 401s. The BootGate's
  // "Starting… / isn't responding" copy is wrong for that — and its overlay
  // (z-1900) would cover the AuthGate dialog (z-1000) — so the gate yields to
  // the token prompt while auth is needed.
  const authNeeded = useSyncExternalStore(subscribeAuth, authRequired);
  // White-labelled gate copy: identity.name → display name (forks read their own).
  const bootName = brandName(runtime?.identity?.name);
  const [bootStuck, setBootStuck] = useState(false);
  useEffect(() => {
    if (bootReady) return; // resolved before the grace period — no timer needed
    const t = window.setTimeout(() => setBootStuck(true), 45_000);
    return () => window.clearTimeout(t);
  }, [bootReady]);
  const [error, setError] = useState("");

  // Clear the stale "Load failed" strip once the engine reports ready. The
  // graph compile (cold start, finishing setup, or a model change) runs inline
  // on the server loop and freezes it, so concurrent pollers fail and set the
  // strip — which is otherwise only cleared by a user action. When `graph_loaded`
  // flips true the connection is healthy again, so that transient error is moot.
  useEffect(() => {
    if (engineReady) setError((prev) => (prev ? "" : prev));
  }, [engineReady]);

  // Adopt the server's default project as the fs working dir if none is set (it
  // seeds the setup wizard's allowed-dirs) once runtime resolves.
  useEffect(() => {
    if (!projectPath.trim() && runtime?.project.path) setProjectPath(runtime.project.path);
  }, [runtime, projectPath, setProjectPath]);


  // Goals now own their data via TanStack Query inside <GoalsPanel> (ADR 0013) —
  // no App-level fetch/poll here.

  // Open the server→client event stream (ADR 0003) and track its connection
  // state for the "live" indicator. Surfaces subscribe to named events.
  useEffect(() => onConnectionChange(setLive), []);

  // Activity unread now lives on the ActivityWidget (the utility-bar widget tracks its own
  // `activity.message` count, clearing it when its dialog opens) — no rail badge to drive.

  // Goal completions surface as a toast (goal.achieved / goal.failed on the bus, ADR 0039) so a
  // plain operator notices a terminal goal without writing a plugin hook.
  const toast = useToast();
  useEffect(() => {
    const offDone = onServerEvent("goal.achieved", (d) =>
      toast({ tone: "success", title: "Goal achieved", message: String(d.condition || "the goal") }));
    const offFail = onServerEvent("goal.failed", (d) =>
      toast({
        tone: "error",
        title: "Goal failed",
        message: `${String(d.condition || "the goal")}${d.reason ? ` — ${String(d.reason)}` : ""}`,
      }));
    return () => { offDone(); offFail(); };
  }, [toast]);

  // Resize + collapse are now the DS AppShell's (controlled via rightWidth/onRightWidthChange +
  // rightCollapsed/onCollapse) — the hand-rolled mouse/keyboard handlers are gone.

  // (The topbar health light was removed — its status/health derivation went with it.
  // Runtime health still surfaces via the warnings strip + the boot gate; full detail
  // lives in Settings ▸ Runtime. SSE connection state is still tracked via `setLive`.)

  // Desktop (macOS) runs with an overlay/invisible title bar — no chrome, the
  // native traffic lights float over the content. Detect that build so the
  // topbar can inset for the lights + act as the window's drag region. (Tauri
  // injects __PROTOAGENT_API_BASE__; the macOS guard avoids insetting on other
  // platforms where the window keeps a normal title bar.)
  const isTauriMac =
    typeof window !== "undefined" &&
    (window.location.protocol === "tauri:" ||
      window.location.hostname === "tauri.localhost" ||
      Boolean((window as unknown as { __PROTOAGENT_API_BASE__?: string }).__PROTOAGENT_API_BASE__)) &&
    /Mac/i.test(navigator.userAgent);

  // ADR 0035 S3 — surface metadata + one renderer, so any surface mounts in either rail.
  // The core surface list lives in coreSurfaces.tsx, shared with the desktop launcher so
  // both build their command lists from one source. Chat is excluded from rail placement
  // here: it mounts unconditionally in its dock's area (streaming continuity) and is
  // movable across all three docks (left/right/bottom) like any other surface.
  // Surfaces for a rail side, in the user's order (railOrder, ADR 0036). Core AND plugin views are
  // first-class railOrder members — reconciled below — so all are reorderable/movable, including
  // Chat. Metadata resolves from core or the live plugin-view set; a freshly-appeared plugin not
  // yet reconciled is appended so it still shows.
  type RailItem = { id: string; label: string; icon: ReactNode };
  const coreMeta = new globalThis.Map<string, RailItem>(CORE_SURFACES.map((s) => [s.id, s] as const));
  const pluginMeta = new globalThis.Map<string, RailItem>(
    allPluginViews.map((v) => [v.key, { id: v.key, label: v.label, icon: pluginViewIcon(v.icon) }] as const),
  );
  const metaFor = (id: string): RailItem | undefined => coreMeta.get(id) ?? pluginMeta.get(id);
  function railSurfaces(side: "left" | "right" | "bottom"): RailItem[] {
    const placed = new Set([...railOrder.left, ...railOrder.right, ...railOrder.bottom]);
    const ordered = (railOrder[side] ?? []).map(metaFor).filter((s): s is RailItem => Boolean(s));
    // Safety net: a plugin view that appeared before reconcile ran — append it for this side.
    const extra = (side === "left" ? pluginRail : side === "right" ? pluginRightPanels : pluginBottom)
      .filter((v) => !placed.has(v.key))
      .map((v): RailItem => ({ id: v.key, label: v.label, icon: pluginViewIcon(v.icon) }));
    // Fork-contributed surfaces (ADR 0038 D3 — the src/ext seam), appended for their rail side.
    // A surface that requiresPlugin is hidden unless that plugin is enabled (lean core).
    const ext = registeredSurfaces()
      .filter((s) => (s.placement ?? "left") === side)
      .filter((s) => !s.requiresPlugin || enabledPluginIds.has(s.requiresPlugin))
      // id "chat" claims the chat SLOT (ADR 0045) — it replaces the core chat surface
      // rather than adding a second rail item.
      .filter((s) => s.id !== "chat")
      .map((s): RailItem => ({ id: s.id, label: s.label, icon: s.icon }));
    return [...ordered, ...extra, ...ext];
  }

  // ── Command palette (⌘K, ADR 0057) ────────────────────────────────────────────
  // Every resolvable View becomes a "go to" command (via openView → setSurface);
  // deep-link actions ride alongside. Plugin-declared `commands:` + inline plugin
  // views are step 3. The registry re-resolves as plugin views appear/disappear.
  const { views: paletteViews } = buildViews({
    core: CORE_SURFACES,
    plugins: allPluginViews.map((v) => ({ key: v.key, label: v.label, icon: pluginViewIcon(v.icon) })),
    ext: registeredSurfaces()
      .filter((s) => s.id !== "chat") // the chat slot isn't a separate surface
      .filter((s) => !s.requiresPlugin || enabledPluginIds.has(s.requiresPlugin))
      .map((s) => ({ id: s.id, label: s.label, icon: s.icon })),
  });
  // Plugin views opted into inline palette morphing (manifest `views[].palette`):
  // ⌘K → the view's command expands its iframe in the palette body, themed + authed
  // via the same handshake PluginView uses. `"inline"` reuses the rail page; an object
  // `{ path }` ships a DISTINCT page for the palette (e.g. a tighter quick editor) vs
  // the full rail panel — so a plugin can serve separate panel and palette views.
  const inlinePaletteViews = allPluginViews
    .filter((v) => v.palette === "inline" || (typeof v.palette === "object" && v.palette !== null))
    .map((v) => ({
      id: v.key,
      title: v.label,
      url: apiUrl(typeof v.palette === "object" && v.palette?.path ? v.palette.path : v.path),
      icon: pluginViewIcon(v.icon),
      theme: consoleTheme(),
      token: authToken(),
      sandbox: "allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox",
    }));
  // Inline chat with the focused agent (ADR 0057) — ⌘K → a quick chat that streams via
  // api.streamChat (ephemeral context per open). Memoized so the transport (+ its
  // session) is stable across renders; re-created only when the agent name changes.
  const chatAgentName = brandName(runtime?.identity?.name);
  const paletteChat = useMemo(
    () => ({
      name: chatAgentName,
      icon: <MessageSquare size={16} />,
      view: {
        id: "chat",
        title: chatAgentName,
        width: 620,
        render: () => <PaletteChat agentName={chatAgentName} />,
      } as PaletteView,
    }),
    [chatAgentName],
  );
  const paletteRegistry = usePaletteRegistry(paletteViews, inlinePaletteViews, paletteChat);
  const [paletteOpen, setPaletteOpen] = useState(false);
  usePaletteHotkey(() => setPaletteOpen((o) => !o));
  // Desktop launcher handoff (ADR 0057): the frameless ⌥Space launcher window can't
  // mutate this window's store, so its navigation commands forward a serializable
  // NavIntent over a Tauri event; the main window replays it here (the Rust shell has
  // already brought this window to the front via `focus_main`).
  useEffect(() => {
    let off = () => {};
    void listen<NavIntent>("palette:navigate", (intent) => applyNavIntent(intent)).then((fn) => {
      off = fn;
    });
    return () => off();
  }, []);

  function renderSurface(id: string): ReactNode {
    switch (id) {
      // The Work hub (2026-06) folds Goals + Tasks(Tasks) + Schedule into one right-rail
      // surface (Overview + tabs). It owns those three panels now — no standalone surfaces.
      case "work":
        return <WorkPanel confirm={setConfirmState} />;
      // Knowledge is the searchable Store; its Memory settings folded into
      // Settings ▸ Workspace ▸ Memory (ADR 0048 S-C).
      case "knowledge":
        return <KnowledgeStore onError={setError} />;
      // Settings is no longer a rail surface (2026-06 consolidation) — it's a utility-bar
      // pill opening the settings dialog (SettingsOverlay). Notes is the first-party `notes`
      // plugin (ADR 0034 S4) — rendered via the default
      // plugin-view case below, not a native surface.
      default: {
        // Fork-contributed surface (src/ext seam, ADR 0038 D3) — rendered in-process.
        // Skip a requiresPlugin surface when its plugin is off (e.g. a stale saved order).
        const ext = registeredSurfaces().find((s) => s.id === id);
        if (ext && (!ext.requiresPlugin || enabledPluginIds.has(ext.requiresPlugin))) return ext.render();
        const v = allPluginViews.find((x) => x.key === id);
        return v ? <PluginView key={v.key} view={v} /> : null;
      }
    }
  }

  // Keep plugin views as first-class railOrder members (ADR 0036) — append new ones, prune gone.
  // Keyed on a stable signature so the effect only fires when the view set actually changes.
  // GATED on the runtime status having RESOLVED: on boot `runtime` is null → zero views →
  // reconcile would prune every persisted `plugin:` entry as "uninstalled", and the loaded set
  // would then re-append them at their MANIFEST placement — wiping the operator's drag-and-drop
  // rail layout on every reload. Unknown ≠ empty: never reconcile against an unresolved list.
  const pluginViewsLoaded = runtime !== null;
  const pluginViewSig = allPluginViews.map((v) => `${v.key}:${v.placement ?? "rail"}`).join(",");
  useEffect(() => {
    if (!pluginViewsLoaded) return;
    reconcilePluginViews([
      ...pluginRail.map((v) => ({ id: v.key, side: "left" as const })),
      ...pluginRightPanels.map((v) => ({ id: v.key, side: "right" as const })),
      ...pluginBottom.map((v) => ({ id: v.key, side: "bottom" as const })),
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pluginViewSig, pluginViewsLoaded, reconcilePluginViews]);

  // Self-heal a persisted railOrder that's missing a CORE surface (e.g. Knowledge lost from a
  // layout saved before it existed / after an IA pass) — railSurfaces() never re-adds a missing
  // core surface, only plugin views. Runs once on mount; idempotent (no-op when the layout is
  // whole), so it can't churn or fight the operator's drag-and-drop order. Unlike plugin views
  // this is NOT gated on runtime — the core set is static, so there's no "unresolved" race.
  useEffect(() => {
    reconcileCoreSurfaces(CORE_SURFACES.map((s) => s.id));
  }, [reconcileCoreSurfaces]);

  // Active surface per rail, clamped to a member of that side (a moved surface never leaves a
  // stale active). Chat is no longer pinned — it lives on whichever rail holds it.
  const leftMembers = railSurfaces("left").map((s) => s.id);
  const rightMembers = railSurfaces("right").map((s) => s.id);
  const leftActive = leftMembers.includes(surface) ? surface : (leftMembers[0] ?? "chat");
  const rightActive = rightMembers.includes(rightPanel) ? rightPanel : (rightMembers[0] ?? "tasks");
  // Bottom dock active surface (mirror left/right) — clamp to a member of the bottom dock.
  const bottomMembers = railSurfaces("bottom").map((s) => s.id);
  const bottomActive = bottomMembers.includes(bottomPanel) ? bottomPanel : (bottomMembers[0] ?? "");
  // Chat mounts unconditionally on whichever dock holds it (streaming continuity, #613) —
  // left, right, OR the bottom dock. Its slot stays mounted while another surface swaps in
  // on the same dock, so an in-flight stream survives a surface switch on any rail.
  const chatRail: "left" | "right" | "bottom" =
    railOrder.right.includes("chat")
      ? "right"
      : railOrder.bottom.includes("chat")
        ? "bottom"
        : "left";
  // Background-stream pulse on the chat rail icon (#613 dot, ADR 0039): show it while a turn
  // streams and chat ISN'T the visible surface on its dock — collapse-aware, any rail. (The
  // old check keyed off the left-only `surface`, so it never pulsed for chat on right/bottom.)
  const chatVisible =
    chatRail === "left"
      ? leftActive === "chat" && !leftCollapsed
      : chatRail === "right"
        ? rightActive === "chat" && !rightCollapsed
        : bottomActive === "chat" && !bottomCollapsed;
  const chatDot = chatStreaming && !chatVisible;

  // Notification dots (ADR 0039): a bus event under `<pluginId>.*` lights that plugin's rail
  // icon until its surface is opened. Subscribe once; refs avoid resubscribing each render.
  const pluginDots = useUI((s) => s.pluginDots);
  const setPluginDot = useUI((s) => s.setPluginDot);
  const pluginKeysRef = useRef<string[]>([]);
  pluginKeysRef.current = allPluginViews.map((v) => v.key);
  // Which plugin surfaces are actually ON SCREEN — so we never dot what the user is looking at, and
  // we clear a dot only when its surface becomes visible. A COLLAPSED right panel is NOT visible
  // even though `rightActive` still names the last-selected panel (and it's persisted), so it must
  // be excluded — otherwise that panel could never light a dot.
  const visibleKeys: string[] = [leftActive, mobileActive];
  if (!rightCollapsed) visibleKeys.push(rightActive);
  if (!bottomCollapsed && bottomActive) visibleKeys.push(bottomActive);
  const activeKeysRef = useRef<Set<string>>(new Set());
  activeKeysRef.current = new Set(visibleKeys);
  useEffect(
    () =>
      onTopic("#", (_data, topic) => {
        const pid = topic.split(".")[0];
        if (!pid) return;
        for (const key of pluginKeysRef.current) {
          // key === `plugin:<pid>:<viewId>`; dot it unless its surface is already on screen.
          if (key.startsWith(`plugin:${pid}:`) && !activeKeysRef.current.has(key)) {
            setPluginDot(key, true);
          }
        }
      }),
    [setPluginDot],
  );
  // Clear a plugin surface's dot once it's actually visible on screen.
  useEffect(() => {
    for (const key of visibleKeys) {
      if (key && key.startsWith("plugin:")) setPluginDot(key, false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [leftActive, rightActive, bottomActive, mobileActive, rightCollapsed, bottomCollapsed, setPluginDot]);

  return (
    <>
    {/* Command palette (⌘K, ADR 0057) — portals over the shell; the same component
        backs the desktop quick-command (step 4). */}
    <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} registry={paletteRegistry} />
    <div className={`app-shell${isTauriMac ? " is-tauri-mac" : ""}`}>
      {/* protoLabs.studio brand bumper — DS Splash (@protolabsai/ui/splash). Holds
          2.5s then hands off via the View Transitions API cross-fade (the
          protoAgent path); shows once per tab session (sessionStorage
          "protoagent.introSeen") and skips under automation. The slotted icon is
          gradient-filled to match the wordmark via stroke="url(#pl-brand-gradient)"
          (the def Splash auto-renders with `gradient`). */}
      <Splash
        logo={<ProtoLabsIcon variant="outline" size={88} decorative gradientStroke tone="accent" />}
        word="protoLabs.studio"
        holdMs={2500}
        once="protoagent.introSeen"
        viewTransition
      />
      {/* Cross-agent awareness: toast + native-notify when ANOTHER agent's turn
          finishes (per-window SSE can't see it — this watches the other slugs'
          persisted in-flight turns and polls their durable tasks via the hub). */}
      <FleetTurnWatch />
      {/* In-app update notice (desktop/Tauri): ambient pill → changelog + Update & Restart. */}
      <UpdateNotice />
      {/* Background subagents (ADR 0050): when a detached job finishes, push its result
          live into the spawning chat (a system message + toast) if it's still open —
          instead of waiting for the next message to surface it. */}
      <BackgroundWatch />
      {/* wait/scheduled resumes (ADR 0053, bd-k02): a server-fired resume turn lands in
          the chat thread; surface it live in the open tab instead of on next interaction. */}
      <ChatResumeWatch />
      {/* Tenant guard: if a DIFFERENT backend now owns this origin (the HUB re-keyed —
          a fork booted on the old port), drop the previous tenant's persisted chat view.
          Keyed on the HUB's uid, NOT the focused agent's — switching fleet agents keeps
          the same hub, so it must not clear chat on a normal swap. */}
      <TenantGuard uid={hostUidQ.data?.instance_uid} />
      {/* Cold-start gate: holds over the app until the runtime probe first
          resolves (engine up), so the ~30s frozen-sidecar boot shows
          "Starting <agent>…" rather than a "Load failed" flash. The DS BootGate
          is a slot-only shell with no `ready` — the host owns the gate by
          conditionally rendering it, and computes the failed/loading copy +
          escape-hatch action. Name flows from identity so forks white-label. */}
      {!bootReady && !authNeeded && (
        // role=status live region restores the screen-reader announcement the old
        // gate carried ("Starting…" / "isn't responding") during the ~30s cold start
        // — the DS BootGate shell doesn't own one. Host wrapper, no CSS (interim
        // for protoContent#203: DS BootGate should own role=status aria-live).
        <div role="status" aria-live="polite">
          <BootGate
            logo={<ProtoLabsIcon variant="outline" size={56} decorative gradientStroke tone="accent" />}
            title={
              bootFailed
                ? `${bootName} isn’t responding`
                : `Starting ${bootName}…`
            }
            detail={
              bootFailed
                ? "The engine didn’t come up in time. It may still be warming up — give it another moment, then retry."
                : bootStuck
                  ? "This is taking longer than usual. The engine may still be compiling, or it may need attention in Settings."
                  : "Warming up the engine — first launch (and finishing setup) can take up to a minute. Later launches are quick."
            }
            action={
              bootFailed ? (
                <Button variant="primary" size="sm" onClick={() => void runtimeQ.refetch()}>
                  Retry
                </Button>
              ) : bootStuck ? (
                <Button variant="primary" size="sm" onClick={() => setBootOverride(true)}>
                  Continue anyway
                </Button>
              ) : null
            }
          />
        </div>
      )}
      {/* Token prompt (#873): any 401 — panel query, boot probe, chat turn — opens
          this. Rendered AFTER the BootGate so a token-gated deployment's first run
          (where the boot probe itself 401s) shows the prompt on top of the gate. */}
      <AuthGate />
      {/* macOS desktop: the topbar IS the window's drag region (its brand insets
          right of the native traffic lights — see `.is-tauri-mac .topbar`).
          Interactive children (the status dot) stay clickable; harmless on web. */}
      {/* White-label top bar — DS Header (protoContent #159). name follows the configured
          identity (Settings ▸ Identity), org is identity.org (falls back to protoLabs.studio),
          logo is the brand mark, status is the glanceable health light. BASE_URL is "/app/" in
          dev and "./" in the desktop build (assets sit at the root). dragRegion = the Tauri
          window drag region. */}
      <div className="app-topbar">
      <Header
        dragRegion
        logo={<ProtoLabsIcon variant="outline" tone="accent" size={22} decorative />}
        name={
          <FleetSwitcher
            fallbackName={brandName(runtime?.identity?.name)}
            onNewAgent={() => {
              // Fleet lives in the Global settings overlay now (header drawer). Open it on
              // the Fleet section + ask the panel to pop the new-agent picker on mount.
              setFleetStartNew(true);
              openGlobalSettings("fleet");
            }}
          />
        }
        org={runtime?.identity?.org || "protoLabs.studio"}
      />
      {/* Top-right hamburger → the app drawer (Global settings, Telemetry, links; mobile nav). */}
      <div className="app-topbar-menu">
        <HamburgerMenu onOpen={() => setDrawerOpen(true)} />
      </div>
      </div>

      {/* Operational warnings from the runtime status (#706 co-located instances etc.) —
          a slim alert strip under the topbar. Server-driven: appears/clears with the poll.
          DS Alert owns the visuals; the class is placement + the e2e hook. */}
      {(runtime?.warnings ?? []).map((w) => (
        <Alert status="warning" className="shell-warning-banner" key={w}>
          {w}
        </Alert>
      ))}

      {/* The dual-rail shell is now the DS AppShell (ADR 0035 + #144): rails (drag-to-reorder +
          cross-rail via dnd-kit), resizable right column, mobile shell, and the utility bar — all
          controlled. We own the surface registry + persistence (railOrder/widths/active in the UI
          store); the shell renders our content + emits callbacks. Chat mounts UNCONDITIONALLY
          (streaming continuity #613) inside the column content, on whichever rail holds it. */}
      <AppShell
        className="app-shell-main"
        leftItems={railSurfaces("left").map((s) => ({
          ...s,
          dot: s.id === "chat" ? chatDot : pluginDots[s.id] || undefined,
        }))}
        rightItems={railSurfaces("right").map((s) => ({
          ...s,
          dot: s.id === "chat" ? chatDot : pluginDots[s.id] || undefined,
        }))}
        bottomItems={railSurfaces("bottom").map((s) => ({
          ...s,
          dot: s.id === "chat" ? chatDot : pluginDots[s.id] || undefined,
        }))}
        activeLeft={leftCollapsed ? "" : leftActive}
        activeRight={rightCollapsed ? "" : rightActive}
        activeBottom={bottomCollapsed ? "" : bottomActive}
        onSelect={(side, id) => {
          // Click the already-open view's icon → toggle the panel closed; otherwise open the
          // panel on the clicked view (re-opening it if it was collapsed).
          if (side === "left") {
            if (!leftCollapsed && leftActive === id) setLeftCollapsed(true);
            else { setSurface(id); setLeftCollapsed(false); }
          } else if (side === "right") {
            if (!rightCollapsed && rightActive === id) setRightCollapsed(true);
            else { setRightPanel(id); setRightCollapsed(false); }
          } else {
            if (!bottomCollapsed && bottomActive === id) setBottomCollapsed(true);
            else { setBottomPanel(id); setBottomCollapsed(false); }
          }
        }}
        onRailContextMenu={(side, e, id) => openContextMenu("rail-surface", e, { id, side })}
        onRailReorder={(next) => {
          // Chat can dock anywhere now — left, right, or the bottom dock. Its slot mounts
          // unconditionally on whichever dock holds it (bottomContent mirrors the side rails),
          // so streaming survives a surface switch on any rail. No bounce-back guard needed.
          setRailOrder(next);
        }}
        rightWidth={rightWidth}
        onRightWidthChange={setRightWidth}
        rightCollapsed={rightCollapsed}
        onCollapse={setRightCollapsed}
        leftCollapsed={leftCollapsed}
        onLeftCollapse={setLeftCollapsed}
        bottomHeight={bottomHeight}
        onBottomHeightChange={setBottomHeight}
        bottomCollapsed={bottomCollapsed}
        onBottomCollapse={setBottomCollapsed}
        // Let the left column narrow to 200 before it snaps/collapses (the DS default is
        // 280, which left a 140–280 dead zone where a narrowed left snapped back up).
        minLeftWidth={200}
        // Mobile bottom bar = the quick-bar (first 5 of quickBarIds). The DS shell's own
        // "More" button is hidden (app-drawer.css) — the unified mobile "more" is the
        // header hamburger's AppDrawer (surfaces + global settings + links). We still pass
        // the full surface set so the bar resolves its quick icons (and the DS sheet would
        // degrade gracefully if ever shown).
        mobileItems={[...railSurfaces("left"), ...railSurfaces("right"), ...railSurfaces("bottom")]
          .map((s) => ({ ...s, dot: pluginDots[s.id] || undefined }))}
        mobileActiveId={mobileActive}
        onMobileSelect={setMobileActive}
        quickBarIds={quickBar}
        leftContent={
          <>
            {error ? (
              <div className="error-strip" role="alert">
                <CircleAlert size={16} />
                <span>{error}</span>
              </div>
            ) : null}
            {chatRail === "left" || isMobile ? (
              <ChatSlot
                onError={setError}
                active={(isMobile ? mobileActive : leftActive) === "chat"}
                pluginView={chatSlotView}
                enabledPluginIds={enabledPluginIds}
              />
            ) : null}
            {(isMobile ? mobileActive : leftActive) !== "chat"
              ? renderSurface(isMobile ? mobileActive : leftActive)
              : null}
          </>
        }
        rightContent={
          <>
            {chatRail === "right" ? (
              <ChatSlot
                onError={setError}
                active={rightActive === "chat"}
                pluginView={chatSlotView}
                enabledPluginIds={enabledPluginIds}
              />
            ) : null}
            {rightActive !== "chat" ? renderSurface(rightActive) : null}
          </>
        }
        bottomContent={
          <>
            {/* Chat on the bottom dock mounts the same way it does on a side rail: the slot
                is always present (hidden when not active) so a switch to another bottom
                surface — or back — never tears down an in-flight stream (#613). */}
            {chatRail === "bottom" ? (
              <ChatSlot
                onError={setError}
                active={bottomActive === "chat"}
                pluginView={chatSlotView}
                enabledPluginIds={enabledPluginIds}
              />
            ) : null}
            {bottomActive && bottomActive !== "chat" ? renderSurface(bottomActive) : null}
          </>
        }
        utilityBar={
          <UtilityBar
            // Bottom-left = widgets, bottom-right = layout (2026-06 IA pass). Docs +
            // GitHub moved into the header drawer.
            start={
              <>
                {/* Settings (far left, 2026-06 consolidation) — opens the one settings dialog
                    (SettingsOverlay). A plain pill, not a UtilityWidget, so the drawer + ⌘K
                    deep-links can open it too via the store flag (openGlobalSettings). */}
                <button
                  type="button"
                  className="util-btn"
                  aria-label="Settings"
                  title="Settings"
                  data-testid="settings-widget"
                  onClick={() => openGlobalSettings()}
                >
                  <Settings2 size={14} />
                </button>
                {/* Widgets (bottom-left): background subagents (ADR 0050 Phase 3), the
                    inbox, and the read-only Activity feed — each a pill with a hover info
                    popover + a click dialog. */}
                <BackgroundJobs />
                <InboxWidget />
                <ActivityWidget />
                {/* Plugin-contributed utility widgets (`views[].utility`): a pill that opens
                    the plugin's iframe in a dialog, with hover info. Reuses PluginView. */}
                {utilityWidgetViews.map((v) => (
                  <UtilityWidget
                    key={v.key}
                    testId={`util-widget-${v.id}`}
                    icon={pluginViewIcon(v.icon, 14)}
                    label={v.label}
                    info={typeof v.utility === "object" && v.utility?.info ? v.utility.info : `Open ${v.label}`}
                    dialogTitle={v.label}
                    dialogWidth="min(900px, 96vw)"
                  >
                    <div className="plugin-widget-dialog">
                      <PluginView view={v} />
                    </div>
                  </UtilityWidget>
                ))}
              </>
            }
            end={
              <>
                <button
                  type="button"
                  className={`util-btn ${bottomCollapsed ? "is-off" : ""}`}
                  onClick={() => setBottomCollapsed(!bottomCollapsed)}
                  disabled={!bottomActive}
                  title={
                    bottomActive
                      ? bottomCollapsed
                        ? "Show bottom panel"
                        : "Hide bottom panel"
                      : "No bottom panel — move a surface to the bottom dock"
                  }
                  aria-label="Toggle bottom panel"
                  data-testid="toggle-bottom"
                >
                  <PanelBottom size={14} />
                </button>
                <button
                  type="button"
                  className={`util-btn ${leftCollapsed ? "is-off" : ""}`}
                  onClick={() => setLeftCollapsed(!leftCollapsed)}
                  title={leftCollapsed ? "Show left panel" : "Hide left panel"}
                  aria-label="Toggle left panel"
                  data-testid="toggle-left"
                >
                  <PanelLeft size={14} />
                </button>
                <button
                  type="button"
                  className={`util-btn ${rightCollapsed ? "is-off" : ""}`}
                  onClick={() => setRightCollapsed(!rightCollapsed)}
                  title={rightCollapsed ? "Show side panel" : "Hide side panel"}
                  aria-label="Toggle side panel"
                  data-testid="toggle-right"
                >
                  <PanelRight size={14} />
                </button>
              </>
            }
          />
        }
      />

      <SetupWizard
        open={runtime?.setup_complete === false}
        projectPath={projectPath}
        onProjectPathChange={setProjectPath}
        onFinished={() => {
          void runtimeQ.refetch();
        }}
      />

      <ConfirmDialog
        open={confirmState !== null}
        title={confirmState?.title ?? ""}
        confirmLabel={confirmState?.confirmLabel ?? "Delete"}
        destructive
        onConfirm={() => {
          confirmState?.onConfirm();
          setConfirmState(null);
        }}
        onClose={() => setConfirmState(null)}
      >
        {confirmState?.message}
      </ConfirmDialog>
    </div>
    {/* App-wide right-click menu (ADR 0036) — one renderer; menus come from the registry.
        Rendered OUTSIDE the .app-shell grid: the DS Menu stays mounted to hold its ref, so
        its (closed) anchor would otherwise be a stray 4th grid row and break the layout. */}
    <ContextMenuRenderer />
    {/* The header drawer (hamburger) — global actions + (on mobile) surface nav. */}
    <AppDrawer
      open={drawerOpen}
      onClose={() => setDrawerOpen(false)}
      mobile={isMobile}
      surfaces={[...railSurfaces("left"), ...railSurfaces("right"), ...railSurfaces("bottom")].map((s) => ({
        id: s.id,
        label: s.label,
        icon: s.icon,
      }))}
      activeSurface={isMobile ? mobileActive : leftActive}
      onSelectSurface={setMobileActive}
      onOpenGlobal={openGlobalSettings}
      version={runtime?.version}
    />
    {/* Global settings overlay — opened from the drawer or a palette deep-link (store-driven). */}
    <SettingsOverlay
      open={globalSettingsOpen}
      section={globalSettingsSection}
      onClose={closeGlobalSettings}
    />
    </>
  );
}


