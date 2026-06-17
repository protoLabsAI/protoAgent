import {
  Activity,
  BarChart3,
  BookMarked,
  Database,
  BookOpen,
  Box,
  Boxes,
  CalendarClock,
  CircleAlert,
  FileText,
  Gauge,
  Github,
  Inbox,
  LayoutDashboard,
  Loader2,
  MessageSquare,
  PanelLeft,
  PanelRight,
  Plus,
  Puzzle,
  Download,
  Store,
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
import { lazy, Suspense, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { ComponentType, LazyExoticComponent, ReactNode } from "react";
import { FleetTurnWatch } from "./FleetTurnWatch";
import { BackgroundWatch } from "./BackgroundWatch";
import { ChatResumeWatch } from "./ChatResumeWatch";
import { BackgroundJobs } from "./BackgroundJobs";
import { ProtoLabsIcon } from "./ProtoLabsIcon";
import { AuthGate } from "./AuthGate";
import { authRequired, subscribeAuth } from "../lib/auth";
import { TenantGuard } from "./TenantGuard";
import { Splash, BootGate } from "@protolabsai/ui/splash";
import { Button } from "@protolabsai/ui/primitives";

import { ActivitySurface } from "../activity/ActivitySurface";
import { ConfirmDialog } from "@protolabsai/ui/overlays";
import { InboxPanel } from "../inbox/InboxPanel";
import { ChatSlot } from "./ChatSlot";
import { useAnyChatStreaming } from "../chat/chat-store";
import { KnowledgeStore } from "../knowledge/KnowledgeStore";
import { SettingsSurface } from "../settings/SettingsSurface";
import { SettingsOverlay } from "../settings/SettingsOverlay";
import { ThemeQuickButton } from "../settings/ThemeQuickButton";
import { FleetSwitcher } from "./FleetSwitcher";
import {
  useUI,
  type ActivityTab,
  type PluginsTab,
  type RightPanel,
  type Surface,
} from "../state/uiStore";
import { api, is401 } from "../lib/api";
import { PluginView } from "./PluginView";
import { AppShell, Header, UtilityBar } from "@protolabsai/ui/app-shell";
import { Alert } from "@protolabsai/ui/data";
import { Logo } from "@protolabsai/ui/primitives";
import { useIsMobile } from "../lib/useIsMobile";
import { useActiveTheme } from "../lib/useActiveTheme";
import { registeredSurfaces } from "../ext"; // build-time fork seam (ADR 0038 D3); also self-loads fork surfaces
import { ContextMenuRenderer, openContextMenu } from "../contextMenu";
import { Tabs } from "@protolabsai/ui/navigation";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { brandName } from "../lib/brand";
import { onConnectionChange, onServerEvent, onTopic } from "../lib/events";
import { useToast } from "@protolabsai/ui/overlays";
import { StatusPill } from "./StatusPill";
import { GoalsPanel } from "./GoalsPanel";
import { BeadsPanel } from "./BeadsPanel";
import { SchedulePanel } from "../schedule/SchedulePanel";
import { BoxSurface } from "./BoxSurface";
import { PluginsSurface } from "../plugins/PluginsSurface";
import { SetupWizard } from "../setup/SetupWizard";
import { hostRuntimeStatusQuery, runtimeStatusQuery } from "../lib/queries";

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
function pluginViewIcon(name?: string): ReactNode {
  if (!name) return <Puzzle size={18} />;
  const Curated = PLUGIN_VIEW_ICONS[name];
  if (Curated) return <Curated size={18} />;
  const Lazy = lazyLucideIcon(toPascalCase(name));
  return (
    <Suspense fallback={<Puzzle size={18} />}>
      <Lazy size={18} />
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

// Adapt the app's sub-tab shape (Lucide icon *component* + optional badge) to the
// DS `Tabs` `TabItem` (icon as a rendered ReactNode) — so call sites keep passing
// `icon: SomeLucideIcon` while the strip renders through @protolabsai/ui.
function toTab(t: { id: string; label: string; icon?: LucideIcon; badge?: ReactNode }) {
  const Icon = t.icon;
  return { id: t.id, label: t.label, icon: Icon ? <Icon size={15} /> : undefined, badge: t.badge };
}

export function App() {
  // Navigation/layout state lives in the persisted UI store (ADR 0035 D5) — a refresh
  // restores the active surface, sub-tabs, and right-panel width/collapse.
  const surface = useUI((s) => s.surface);
  const setSurface = useUI((s) => s.setSurface);
  // Background-streaming indicator for the Chat rail (narrow selector → only
  // re-renders when the boolean flips, not per token).
  const chatStreaming = useAnyChatStreaming();
  const pluginsTab = useUI((s) => s.pluginsTab);
  const setPluginsTab = useUI((s) => s.setPluginsTab);
  const setBoxTab = useUI((s) => s.setBoxTab);
  const setFleetStartNew = useUI((s) => s.setFleetStartNew);
  const activityTab = useUI((s) => s.activityTab);
  const setActivityTab = useUI((s) => s.setActivityTab);
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
  const isMobile = useIsMobile();
  useActiveTheme(); // apply the focused agent's saved theme on boot + repaint on switch (ADR 0042)
  const mobileActive = useUI((s) => s.mobileActive);
  const setMobileActive = useUI((s) => s.setMobileActive);
  const quickBar = useUI((s) => s.quickBar);
  const setRailOrder = useUI((s) => s.setRailOrder);
  const [live, setLive] = useState(false);
  // Shared custom confirm for destructive actions (notes/beads delete).
  const [confirmState, setConfirmState] = useState<
    null | { title: string; message?: string; confirmLabel?: string; onConfirm: () => void }
  >(null);
  const [activityUnread, setActivityUnread] = useState(0);
  const [inboxUnread, setInboxUnread] = useState(0);
  // The one-stop-shop Settings overlay (ADR 0048) — opened by the topbar gear.
  const [settingsOverlayOpen, setSettingsOverlayOpen] = useState(false);
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
  // "right" (a right-sidebar panel, alongside Notes/Beads/Goals/Schedule — ADR 0026).
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
  const allPluginViews = allDeclaredViews.filter((v) => v.slot !== "chat");
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

  // Unread badges (Activity rail + its Inbox sub-tab): count agent-initiated
  // messages / inbound items that arrive while the operator isn't looking at
  // the matching view. Refs so the event handlers read the live view.
  const surfaceRef = useRef(surface);
  surfaceRef.current = surface;
  const activityTabRef = useRef(activityTab);
  activityTabRef.current = activityTab;
  const viewingThread = () => surfaceRef.current === "activity" && activityTabRef.current === "thread";
  const viewingInbox = () => surfaceRef.current === "activity" && activityTabRef.current === "inbox";

  useEffect(
    () =>
      onServerEvent("activity.message", () => {
        if (!viewingThread()) setActivityUnread((n) => n + 1);
      }),
    [],
  );
  useEffect(() => {
    if (viewingThread()) setActivityUnread(0);
  }, [surface, activityTab]);

  useEffect(
    () =>
      onServerEvent("inbox.item", () => {
        if (!viewingInbox()) setInboxUnread((n) => n + 1);
      }),
    [],
  );

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
  useEffect(() => {
    if (viewingInbox()) setInboxUnread(0);
  }, [surface, activityTab]);

  // Resize + collapse are now the DS AppShell's (controlled via rightWidth/onRightWidthChange +
  // rightCollapsed/onCollapse) — the hand-rolled mouse/keyboard handlers are gone.

  // One glanceable health light for the topbar (detail on hover; full status in
  // System → Runtime). Worst-state wins. Derived from the runtime query — while
  // it's still loading (no data, e.g. the sidecar booting) we show "starting".
  const statusLabel = runtimeQ.isError
    ? "error"
    : !runtime
      ? "starting server…"
      : runtimeQ.isFetching
        ? "refreshing"
        : "ready";
  const health: { tone: "ok" | "warning" | "error"; label: string } =
    !runtime && runtimeQ.isError ? { tone: "error", label: "error" }
    : !runtime ? { tone: "warning", label: "starting…" }
    : !runtime.setup_complete ? { tone: "warning", label: "setup pending" }
    : !runtime.graph_loaded ? { tone: "error", label: "graph offline" }
    : { tone: "ok", label: "ready" };

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
  // Chat is excluded here: it mounts unconditionally in its rail's area (streaming continuity)
  // and is pinned left (railOf.chat is never moved).
  const CORE_SURFACES: { id: string; label: string; icon: ReactNode }[] = [
    { id: "chat", label: "Chat", icon: <MessageSquare size={18} /> },
    { id: "activity", label: "Activity", icon: <Activity size={18} /> },
    // "studio" (Workflows) is contributed via src/ext/workflows.tsx, gated on the
    // workflows plugin (lean core) — no longer a hardcoded core surface.
    { id: "knowledge", label: "Knowledge", icon: <BookMarked size={18} /> },
    // "agent" folded into Settings ▸ Workspace (ADR 0048 S-C) — no longer a rail surface.
    { id: "plugins", label: "Plugins", icon: <Puzzle size={18} /> },
    // Box (PR4) — box-level ops (Fleet · Telemetry · Commons), moved out of Settings ▸ Global.
    { id: "box", label: "Box", icon: <Box size={18} /> },
    { id: "settings", label: "Settings", icon: <Settings2 size={18} /> },
    { id: "beads", label: "Beads", icon: <Boxes size={18} /> },
    { id: "goals", label: "Goals", icon: <Target size={18} /> },
    // "schedule" is now a tab inside the Activity surface (#1075), not a rail surface.
  ];
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

  function renderSurface(id: string): ReactNode {
    switch (id) {
      case "activity":
        return (
          <>
            {/* Schedule is a third Activity tab (#1075): cron is a TRIGGER, so timed
                turns belong alongside the thread + inbox triggers, not in their own rail. */}
            <Tabs responsive active={activityTab} onSelect={(t) => setActivityTab(t as ActivityTab)} items={[
              { id: "thread", label: "Thread", icon: Activity },
              { id: "inbox", label: "Inbox", icon: Inbox, badge: inboxUnread ? (<span data-testid="inbox-badge">{inboxUnread > 9 ? "9+" : inboxUnread}</span>) : null },
              { id: "schedule", label: "Schedule", icon: CalendarClock },
            ].map(toTab)} />
            {activityTab === "thread" ? <ActivitySurface onError={setError} /> : activityTab === "inbox" ? <InboxPanel /> : <SchedulePanel />}
          </>
        );
      // The Agent surface folded into Settings ▸ Workspace (ADR 0048 S-C). Its tabs
      // (Identity/Settings/Tools/MCP/Subagents/Skills/Middleware) are now Workspace
      // sections in SettingsSurface.
      case "plugins":
        return (
          <>
            <Tabs responsive active={pluginsTab} onSelect={(t) => setPluginsTab(t as PluginsTab)} items={[
              { id: "local", label: "Local", icon: Boxes },
              { id: "market", label: "Market", icon: Store },
              { id: "download", label: "Download", icon: Download },
            ].map(toTab)} />
            <PluginsSurface tab={pluginsTab} />
          </>
        );
      // Knowledge is the searchable Store; its Memory settings folded into
      // Settings ▸ Workspace ▸ Memory (ADR 0048 S-C).
      case "knowledge":
        return <KnowledgeStore onError={setError} />;
      case "settings":
        // SettingsSurface owns its own two-level nav (home + section, ADR 0048).
        return <SettingsSurface />;
      // Box (PR4) — owns its own Fleet/Telemetry/Commons sub-tabs.
      case "box":
        return <BoxSurface />;
      // Notes is now the first-party `notes` plugin (ADR 0034 S4) — rendered via the default
      // plugin-view case below, not a native surface.
      case "beads":
        return <BeadsPanel confirm={setConfirmState} />;
      case "goals":
        return <GoalsPanel />;
      // "schedule" folded into the Activity surface as a 3rd tab (#1075) — no rail surface.
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

  // Active surface per rail, clamped to a member of that side (a moved surface never leaves a
  // stale active). Chat is no longer pinned — it lives on whichever rail holds it.
  const leftMembers = railSurfaces("left").map((s) => s.id);
  const rightMembers = railSurfaces("right").map((s) => s.id);
  const leftActive = leftMembers.includes(surface) ? surface : (leftMembers[0] ?? "chat");
  const rightActive = rightMembers.includes(rightPanel) ? rightPanel : (rightMembers[0] ?? "beads");
  // Bottom dock active surface (mirror left/right) — clamp to a member of the bottom dock.
  const bottomMembers = railSurfaces("bottom").map((s) => s.id);
  const bottomActive = bottomMembers.includes(bottomPanel) ? bottomPanel : (bottomMembers[0] ?? "");
  // Chat mounts unconditionally on whichever side it's on (streaming continuity, #613).
  const chatRail: "left" | "right" = railOrder.right.includes("chat") ? "right" : "left";

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
    <div className={`app-shell${isTauriMac ? " is-tauri-mac" : ""}`}>
      {/* protoLabs.studio brand bumper — DS Splash (@protolabsai/ui/splash). Holds
          2.5s then hands off via the View Transitions API cross-fade (the
          protoAgent path); shows once per tab session (sessionStorage
          "protoagent.introSeen") and skips under automation. The slotted icon is
          gradient-filled to match the wordmark via stroke="url(#pl-brand-gradient)"
          (the def Splash auto-renders with `gradient`). */}
      <Splash
        logo={<ProtoLabsIcon variant="outline" size={88} decorative gradientStroke />}
        word="protoLabs.studio"
        holdMs={2500}
        once="protoagent.introSeen"
        viewTransition
      />
      {/* Cross-agent awareness: toast + native-notify when ANOTHER agent's turn
          finishes (per-window SSE can't see it — this watches the other slugs'
          persisted in-flight turns and polls their durable tasks via the hub). */}
      <FleetTurnWatch />
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
            logo={<ProtoLabsIcon variant="outline" size={56} decorative />}
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
        logo={<Logo src={`${import.meta.env.BASE_URL}protolabs-icon-outline.svg`} alt="" size={22} />}
        name={
          <FleetSwitcher
            fallbackName={brandName(runtime?.identity?.name)}
            onNewAgent={() => {
              // Fleet now lives under the Box surface (PR4); ask the panel to open
              // the new-agent picker on mount.
              setSurface("box");
              setBoxTab("fleet");
              setFleetStartNew(true);
            }}
          />
        }
        org={runtime?.identity?.org || "protoLabs.studio"}
        actions={
          <>
            {/* Quick-settings (ADR 0048). Model tuning lives on the chat composer (by the
                input); the topbar keeps appearance + the gear to the full one-stop-shop. */}
            <ThemeQuickButton />
            <Button
              icon
              variant="ghost"
              type="button"
              title="Open settings"
              aria-label="Open settings"
              data-testid="topbar-settings"
              onClick={() => setSettingsOverlayOpen(true)}
            >
              <Settings2 size={16} />
            </Button>
          </>
        }
        status={
          <button
            type="button"
            className={`status-dot tone-${health.tone}`}
            onClick={() => {
              void runtimeQ.refetch();
            }}
            title={
              `Setup: ${runtime?.setup_complete ? "complete" : "pending"}\n` +
              `Graph: ${runtime?.graph_loaded ? "loaded" : "offline"}\n` +
              `Event stream: ${live ? "connected" : "offline"}\n` +
              `Status: ${statusLabel}` +
              (error ? `\nError: ${error}` : "") +
              `\n\nClick to refresh.`
            }
            aria-label={`Status: ${health.label}. Click to refresh.`}
            data-testid="live-indicator"
            data-live={live ? "true" : "false"}
          />
        }
      />
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
          badge: s.id === "activity" ? activityUnread + inboxUnread : undefined,
          dot: s.id === "chat" ? chatStreaming && surface !== "chat" : pluginDots[s.id] || undefined,
        }))}
        rightItems={railSurfaces("right").map((s) => ({ ...s, dot: pluginDots[s.id] || undefined }))}
        bottomItems={railSurfaces("bottom").map((s) => ({ ...s, dot: pluginDots[s.id] || undefined }))}
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
          // Chat is the streaming slot (#613), never the bottom dock — if a drag landed it
          // there, bounce it back to the SIDE rail it came from (its pre-drag side), not
          // always the left.
          if (next.bottom.includes("chat")) {
            const back = railOrder.right.includes("chat") ? "right" : "left";
            next = { ...next, bottom: next.bottom.filter((x) => x !== "chat") };
            if (!next[back].includes("chat")) next = { ...next, [back]: [...next[back], "chat"] };
          }
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
        mobileItems={[...railSurfaces("left"), ...railSurfaces("right"), ...railSurfaces("bottom")].map((s) => ({
          ...s,
          dot: pluginDots[s.id] || undefined,
        }))}
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
        bottomContent={bottomActive ? renderSurface(bottomActive) : null}
        utilityBar={
          <UtilityBar
            start={
              <>
                <a className="util-btn" href="https://protolabsai.github.io/protoAgent/" target="_blank" rel="noreferrer" title="Documentation" aria-label="Documentation">
                  <BookOpen size={14} />
                </a>
                <a className="util-btn" href="https://github.com/protoLabsAI/protoAgent" target="_blank" rel="noreferrer" title="GitHub repository" aria-label="GitHub repository">
                  <Github size={14} />
                </a>
              </>
            }
            end={
              <>
                {/* Background subagents (ADR 0050 Phase 3) — live pill + jobs dialog. */}
                <BackgroundJobs />
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
    {/* The one-stop-shop Settings overlay (ADR 0048) — the topbar gear opens it. */}
    <SettingsOverlay open={settingsOverlayOpen} onClose={() => setSettingsOverlayOpen(false)} />
    </>
  );
}


