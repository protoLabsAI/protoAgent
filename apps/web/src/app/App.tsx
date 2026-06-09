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
  Github,
  Inbox,
  LayoutDashboard,
  Loader2,
  MessageSquare,
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
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import type { ComponentType, CSSProperties, LazyExoticComponent, ReactNode } from "react";
import { IntroSplash } from "./IntroSplash";
import { BootGate } from "./BootGate";

import { ActivitySurface } from "../activity/ActivitySurface";
import { ConfirmDialog } from "@protolabsai/ui";
import { InboxPanel } from "../inbox/InboxPanel";
import { ChatSurface } from "../chat/ChatSurface";
import { useAnyChatStreaming } from "../chat/chat-store";
import { KnowledgeStore } from "../knowledge/KnowledgeStore";
import { PlaybooksSurface } from "../playbooks/PlaybooksSurface";
import { SettingsSurface, SETTINGS_TABS, type SettingsTab } from "../settings/SettingsSurface";
import {
  useUI,
  type ActivityTab,
  type AgentTab,
  type KnowledgeTab,
  type PluginsTab,
  type RightPanel,
  type Surface,
} from "../state/uiStore";
import { SettingsCategoryPanel } from "../settings/SettingsCategory";
import { WorkflowsSurface } from "../workflows/WorkflowsSurface";
import { api } from "../lib/api";
import { PluginView } from "./PluginView";
import { SurfaceRail } from "../components/SurfaceRail";
import { MobileNav } from "../components/MobileNav";
import { useIsMobile } from "../lib/useIsMobile";
import { registeredSurfaces } from "../ext"; // build-time fork seam (ADR 0038 D3); also self-loads fork surfaces
import { ContextMenuRenderer, openContextMenu } from "../contextMenu";
import { Tabs } from "@protolabsai/ui";
import { PanelHeader } from "@protolabsai/ui";
import { brandName } from "../lib/brand";
import { onConnectionChange, onServerEvent, onTopic } from "../lib/events";
import { useToast } from "@protolabsai/ui";
import { StatusPill } from "./StatusPill";
import { GoalsPanel } from "./GoalsPanel";
import { BeadsPanel } from "./BeadsPanel";
import { SchedulePanel } from "../schedule/SchedulePanel";
import { IdentityPanel } from "../agent/IdentityPanel";
import { ToolsPanel } from "./ToolsPanel";
import { McpPanel } from "./McpPanel";
import { SubagentsPanel } from "./SubagentsPanel";
import { MiddlewarePanel } from "./MiddlewarePanel";
import { PluginsSurface } from "../plugins/PluginsSurface";
import { SetupWizard } from "../setup/SetupWizard";
import { runtimeStatusQuery } from "../lib/queries";

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
  const agentTab = useUI((s) => s.agentTab);
  const setAgentTab = useUI((s) => s.setAgentTab);
  const pluginsTab = useUI((s) => s.pluginsTab);
  const setPluginsTab = useUI((s) => s.setPluginsTab);
  const knowledgeTab = useUI((s) => s.knowledgeTab);
  const setKnowledgeTab = useUI((s) => s.setKnowledgeTab);
  const settingsTab = useUI((s) => s.settingsTab);
  const setSettingsTab = useUI((s) => s.setSettingsTab);
  const activityTab = useUI((s) => s.activityTab);
  const setActivityTab = useUI((s) => s.setActivityTab);
  const rightPanel = useUI((s) => s.rightPanel);
  const setRightPanel = useUI((s) => s.setRightPanel);
  const rightCollapsed = useUI((s) => s.rightCollapsed);
  const setRightCollapsed = useUI((s) => s.setRightCollapsed);
  const rightWidth = useUI((s) => s.rightWidth);
  const setRightWidth = useUI((s) => s.setRightWidth);
  const railOrder = useUI((s) => s.railOrder);
  const reconcilePluginViews = useUI((s) => s.reconcilePluginViews);
  const isMobile = useIsMobile();
  const mobileActive = useUI((s) => s.mobileActive);
  const setMobileActive = useUI((s) => s.setMobileActive);
  const quickBar = useUI((s) => s.quickBar);
  const [live, setLive] = useState(false);
  // Shared custom confirm for destructive actions (notes/beads delete).
  const [confirmState, setConfirmState] = useState<
    null | { title: string; message?: string; confirmLabel?: string; onConfirm: () => void }
  >(null);
  const [activityUnread, setActivityUnread] = useState(0);
  const [inboxUnread, setInboxUnread] = useState(0);
  const [projectPath, setProjectPath] = useLocalStorageState("protoagent.projectPath", "");
  // Shell-level runtime read (ADR 0013): non-suspense useQuery so the topbar
  // always renders; the retry doubles as the desktop sidecar boot-probe. The
  // System → Runtime panel reads the same key via useSuspenseQuery. Keep polling
  // until the graph is compiled (`graph_loaded`) so the BootGate observes the
  // engine coming up — the post-setup compile runs inline on the server loop and
  // briefly freezes it, so we want to notice the moment it's live again.
  const runtimeQ = useQuery({
    ...runtimeStatusQuery(),
    retry: 30,
    retryDelay: 1000,
    refetchInterval: (q) => (q.state.data?.graph_loaded ? false : 2500),
  });
  const runtime = runtimeQ.data ?? null;

  // Plugin-contributed rail surfaces (ADR 0026): each enabled plugin's declared
  // views become a dynamic rail icon (keyed plugin:<id>:<viewId>) whose panel is
  // an iframe of the page the plugin serves. PR1 thin vertical — PR2 generalizes
  // the rail into a full registry.
  // A plugin view declares its placement: "rail" (default — a left-rail surface) or
  // "right" (a right-sidebar panel, alongside Notes/Beads/Goals/Schedule — ADR 0026).
  const allPluginViews = (runtime?.plugins ?? [])
    .filter((p) => p.enabled && p.views?.length)
    .flatMap((p) => (p.views ?? []).map((v) => ({ ...v, key: `plugin:${p.id}:${v.id}` })));
  const pluginRail = allPluginViews.filter((v) => (v.placement ?? "rail") !== "right");
  const pluginRightPanels = allPluginViews.filter((v) => v.placement === "right");
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

  // Drag the right panel's left edge to resize (clamped 280–720px, persisted).
  function startRightResize(e: React.MouseEvent) {
    e.preventDefault();
    const startX = e.clientX;
    const startW = rightWidth;
    const onMove = (ev: MouseEvent) => {
      const next = Math.min(720, Math.max(280, startW + (startX - ev.clientX)));
      setRightWidth(next);
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      document.body.style.userSelect = "";
    };
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  // Keyboard-resizable (ADR 0035 S3): arrows nudge, Home/End jump to max/min, double-click
  // resets. The handle sits on the right panel's LEFT edge, so ← widens / → narrows.
  const RIGHT_DEFAULT_WIDTH = 360;
  function onResizeKey(e: React.KeyboardEvent) {
    const step = e.shiftKey ? 48 : 16;
    if (e.key === "ArrowLeft") { setRightWidth(rightWidth + step); e.preventDefault(); }
    else if (e.key === "ArrowRight") { setRightWidth(rightWidth - step); e.preventDefault(); }
    else if (e.key === "Home") { setRightWidth(720); e.preventDefault(); }
    else if (e.key === "End") { setRightWidth(280); e.preventDefault(); }
  }

  // Drive only the right column's WIDTH via a CSS var — the grid template
  // itself lives in CSS (.workspace), so the responsive media query can
  // collapse to two columns below the breakpoint. Setting the full template
  // inline here would beat the media query and leave a blank reserved column.
  const rightCol = rightCollapsed ? "0px" : `${rightWidth}px`;

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
    { id: "studio", label: "Studio", icon: <Boxes size={18} /> },
    { id: "knowledge", label: "Knowledge", icon: <BookMarked size={18} /> },
    { id: "agent", label: "Agent", icon: <Bot size={18} /> },
    { id: "plugins", label: "Plugins", icon: <Puzzle size={18} /> },
    { id: "settings", label: "Settings", icon: <Settings2 size={18} /> },
    { id: "beads", label: "Beads", icon: <Boxes size={18} /> },
    { id: "goals", label: "Goals", icon: <Target size={18} /> },
    { id: "schedule", label: "Schedule", icon: <CalendarClock size={18} /> },
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
  function railSurfaces(side: "left" | "right"): RailItem[] {
    const placed = new Set([...railOrder.left, ...railOrder.right]);
    const ordered = (railOrder[side] ?? []).map(metaFor).filter((s): s is RailItem => Boolean(s));
    // Safety net: a plugin view that appeared before reconcile ran — append it for this side.
    const extra = (side === "left" ? pluginRail : pluginRightPanels)
      .filter((v) => !placed.has(v.key))
      .map((v): RailItem => ({ id: v.key, label: v.label, icon: pluginViewIcon(v.icon) }));
    // Fork-contributed surfaces (ADR 0038 D3 — the src/ext seam), appended for their rail side.
    const ext = registeredSurfaces()
      .filter((s) => (s.placement ?? "left") === side)
      .map((s): RailItem => ({ id: s.id, label: s.label, icon: s.icon }));
    return [...ordered, ...extra, ...ext];
  }

  function renderSurface(id: string): ReactNode {
    switch (id) {
      case "activity":
        return (
          <>
            <Tabs active={activityTab} onSelect={(t) => setActivityTab(t as ActivityTab)} items={[
              { id: "thread", label: "Thread", icon: Activity },
              { id: "inbox", label: "Inbox", icon: Inbox, badge: inboxUnread ? (<span data-testid="inbox-badge">{inboxUnread > 9 ? "9+" : inboxUnread}</span>) : null },
            ].map(toTab)} />
            {activityTab === "thread" ? <ActivitySurface onError={setError} /> : <InboxPanel />}
          </>
        );
      case "studio":
        return <WorkflowsSurface />;
      case "agent":
        return (
          <>
            <Tabs active={agentTab} onSelect={(t) => setAgentTab(t as AgentTab)} items={[
              { id: "identity", label: "Identity", icon: Sparkles },
              { id: "settings", label: "Settings", icon: Settings2 },
              { id: "tools", label: "Tools", icon: Wrench },
              { id: "mcp", label: "MCP", icon: Plug },
              { id: "subagents", label: "Subagents", icon: Bot },
              { id: "skills", label: "Skills", icon: BookMarked },
              { id: "middleware", label: "Middleware", icon: Layers },
            ].map(toTab)} />
            {agentTab === "identity" ? <IdentityPanel /> : null}
            {agentTab === "settings" ? <SettingsCategoryPanel category="Agent" title="Settings" /> : null}
            {agentTab === "tools" ? <ToolsPanel /> : null}
            {agentTab === "mcp" ? <McpPanel /> : null}
            {agentTab === "subagents" ? <SubagentsPanel /> : null}
            {agentTab === "skills" ? <PlaybooksSurface onError={setError} /> : null}
            {agentTab === "middleware" ? <MiddlewarePanel /> : null}
          </>
        );
      case "plugins":
        return (
          <>
            <Tabs active={pluginsTab} onSelect={(t) => setPluginsTab(t as PluginsTab)} items={[
              { id: "local", label: "Local", icon: Boxes },
              { id: "market", label: "Market", icon: Store },
              { id: "download", label: "Download", icon: Download },
            ].map(toTab)} />
            <PluginsSurface tab={pluginsTab} />
          </>
        );
      case "knowledge":
        return (
          <>
            <Tabs active={knowledgeTab} onSelect={(t) => setKnowledgeTab(t as KnowledgeTab)} items={[
              { id: "store", label: "Store", icon: Database },
              { id: "settings", label: "Settings", icon: Settings2 },
            ].map(toTab)} />
            {knowledgeTab === "store" ? <KnowledgeStore onError={setError} /> : <SettingsCategoryPanel category="Memory" title="Settings" />}
          </>
        );
      case "settings":
        return (
          <>
            <Tabs active={settingsTab} onSelect={(t) => setSettingsTab(t as SettingsTab)} items={SETTINGS_TABS.map(toTab)} />
            <SettingsSurface tab={settingsTab} />
          </>
        );
      // Notes is now the first-party `notes` plugin (ADR 0034 S4) — rendered via the default
      // plugin-view case below, not a native surface.
      case "beads":
        return <BeadsPanel confirm={setConfirmState} />;
      case "goals":
        return <GoalsPanel />;
      case "schedule":
        return <SchedulePanel />;
      default: {
        // Fork-contributed surface (src/ext seam, ADR 0038 D3) — rendered in-process.
        const ext = registeredSurfaces().find((s) => s.id === id);
        if (ext) return ext.render();
        const v = allPluginViews.find((x) => x.key === id);
        return v ? <PluginView key={v.key} view={v} /> : null;
      }
    }
  }

  // Keep plugin views as first-class railOrder members (ADR 0036) — append new ones, prune gone.
  // Keyed on a stable signature so the effect only fires when the view set actually changes.
  const pluginViewSig = allPluginViews.map((v) => `${v.key}:${v.placement ?? "rail"}`).join(",");
  useEffect(() => {
    reconcilePluginViews([
      ...pluginRail.map((v) => ({ id: v.key, side: "left" as const })),
      ...pluginRightPanels.map((v) => ({ id: v.key, side: "right" as const })),
    ]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pluginViewSig, reconcilePluginViews]);

  // Active surface per rail, clamped to a member of that side (a moved surface never leaves a
  // stale active). Chat is no longer pinned — it lives on whichever rail holds it.
  const leftMembers = railSurfaces("left").map((s) => s.id);
  const rightMembers = railSurfaces("right").map((s) => s.id);
  const leftActive = leftMembers.includes(surface) ? surface : (leftMembers[0] ?? "chat");
  const rightActive = rightMembers.includes(rightPanel) ? rightPanel : (rightMembers[0] ?? "beads");
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
  }, [leftActive, rightActive, mobileActive, rightCollapsed, setPluginDot]);

  return (
    <div className={`app-shell${isTauriMac ? " is-tauri-mac" : ""}`}>
      <IntroSplash />
      {/* App-wide right-click menu (ADR 0036) — one renderer; menus come from the registry. */}
      <ContextMenuRenderer />
      {/* Cold-start gate: holds over the app until the runtime probe first
          resolves (engine up), so the ~30s frozen-sidecar boot shows
          "Starting <agent>…" rather than a "Load failed" flash. */}
      <BootGate
        ready={bootReady}
        failed={!runtime && runtimeQ.isError}
        name={brandName(runtime?.identity?.name)}
        onRetry={() => void runtimeQ.refetch()}
        onContinue={() => setBootOverride(true)}
      />
      {/* macOS desktop: the topbar IS the window's drag region (its brand insets
          right of the native traffic lights — see `.is-tauri-mac .topbar`).
          Interactive children (the status dot) stay clickable; harmless on web. */}
      <header className="topbar" data-tauri-drag-region>
        <div className="brand-lockup">
          {/* BASE_URL is "/app/" in dev and "./" in the desktop build — a
              hardcoded "/app/…" 404s in the bundle (assets sit at the root). */}
          <img src={`${import.meta.env.BASE_URL}protolabs-icon-outline.svg`} alt="" className="brand-mark" />
          <div>
            {/* White-label: the brand name follows the configured identity
                (Settings → Identity), defaulting to protoAgent for the template.
                A fork sets its name once and the whole UI follows. */}
            <div className="brand-name">{brandName(runtime?.identity?.name)}</div>
            <div className="brand-subline">protoLabs.studio</div>
          </div>
        </div>
        <div className="topbar-status">
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
        </div>
      </header>

      {isMobile ? (
        /* Mobile shell (ADR 0035 S4): one surface at a time + a bottom quick-bar + hamburger; no
           rails, no split. Chat mounts unconditionally for streaming continuity. */
        <div className="workspace mobile">
          <main className="stage">
            <ChatSurface onError={setError} active={mobileActive === "chat"} />
            {mobileActive !== "chat" ? renderSurface(mobileActive) : null}
          </main>
          <MobileNav
            items={[...railSurfaces("left"), ...railSurfaces("right")].map((s) => ({
              ...s,
              dot: pluginDots[s.id] || undefined,
            }))}
            activeId={mobileActive}
            onSelect={setMobileActive}
            quickBarIds={quickBar}
          />
        </div>
      ) : (
      <div
        className={`workspace ${rightCollapsed ? "right-collapsed" : ""}`}
        style={{ "--right-width": rightCol } as CSSProperties}
      >
        {/* Left rail (ADR 0035/0036) — members + order from railOrder; right-click a surface to
            reorder or move it across. The rail is the extraction-ready <SurfaceRail>. */}
        <SurfaceRail
          side="left"
          ariaLabel="Workspace surfaces"
          items={railSurfaces("left").map((s) => ({
            ...s,
            badge: s.id === "activity" ? activityUnread + inboxUnread : undefined,
            dot: s.id === "chat" ? chatStreaming && surface !== "chat" : pluginDots[s.id] || undefined,
          }))}
          activeId={leftActive}
          onSelect={(id) => setSurface(id)}
          onContextMenu={(e, id) => openContextMenu("rail-surface", e, { id, side: "left" })}
        />

        <main className="stage">
          {error ? (
            <div className="error-strip" role="alert">
              <CircleAlert size={16} />
              <span>{error}</span>
            </div>
          ) : null}

          {/* Chat mounts UNCONDITIONALLY + hidden via `active` (streaming continuity, #613);
              it's pinned to the left rail. Every other left-rail surface renders through the
              shared renderSurface (ADR 0035 S3) — so it can live on either rail. */}
          {chatRail === "left" ? <ChatSurface onError={setError} active={leftActive === "chat"} /> : null}
          {leftActive !== "chat" ? renderSurface(leftActive) : null}
        </main>

        <aside className="right-panel">
          {!rightCollapsed ? (
            <div
              className="resize-handle"
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize side panel (arrows to nudge, double-click to reset)"
              aria-valuenow={rightWidth}
              aria-valuemin={280}
              aria-valuemax={720}
              tabIndex={0}
              onMouseDown={startRightResize}
              onKeyDown={onResizeKey}
              onDoubleClick={() => setRightWidth(RIGHT_DEFAULT_WIDTH)}
              data-testid="right-resize"
            />
          ) : null}
          {/* The right surface renders through the same renderSurface as the left (ADR 0035 S3).
              If Chat lives on this rail it mounts unconditionally (continuity), like the stage. */}
          {chatRail === "right" ? <ChatSurface onError={setError} active={rightActive === "chat"} /> : null}
          {!rightCollapsed && rightActive !== "chat" ? renderSurface(rightActive) : null}
        </aside>

        {/* Right rail (ADR 0035/0036) — mirrors the left on the far edge; same <SurfaceRail>. */}
        <SurfaceRail
          side="right"
          ariaLabel="Context surfaces"
          items={railSurfaces("right").map((s) => ({ ...s, dot: pluginDots[s.id] || undefined }))}
          activeId={rightCollapsed ? "" : rightActive}
          onSelect={(id) => { setRightPanel(id); setRightCollapsed(false); }}
          onContextMenu={(e, id) => openContextMenu("rail-surface", e, { id, side: "right" })}
        />
      </div>
      )}

      <footer className="utility-bar">
        <a
          className="util-btn"
          href="https://protolabsai.github.io/protoAgent/"
          target="_blank"
          rel="noreferrer"
          title="Documentation"
          aria-label="Documentation"
        >
          <BookOpen size={14} />
        </a>
        <a
          className="util-btn"
          href="https://github.com/protoLabsAI/protoAgent"
          target="_blank"
          rel="noreferrer"
          title="GitHub repository"
          aria-label="GitHub repository"
        >
          <Github size={14} />
        </a>
        <div className="util-spacer" />
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
      </footer>

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
  );
}


