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
  Save,
  Settings2,
  Sparkles,
  Target,
  Undo2,
  Trash2,
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
import { useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { IntroSplash } from "./IntroSplash";
import { BootGate } from "./BootGate";

import { ActivitySurface } from "../activity/ActivitySurface";
import { ConfirmDialog } from "./ConfirmDialog";
import { InboxPanel } from "../inbox/InboxPanel";
import { ChatSurface } from "../chat/ChatSurface";
import { useAnyChatStreaming } from "../chat/chat-store";
import { KnowledgeStore } from "../knowledge/KnowledgeStore";
import { PlaybooksSurface } from "../playbooks/PlaybooksSurface";
import { SettingsSurface } from "../settings/SettingsSurface";
import { TelemetrySurface } from "../telemetry/TelemetrySurface";
import { WorkflowsSurface } from "../workflows/WorkflowsSurface";
import { api } from "../lib/api";
import { PluginView } from "./PluginView";
import { brandName } from "../lib/brand";
import { onConnectionChange, onServerEvent } from "../lib/events";
import type { NotesWorkspace } from "../lib/types";
import { StatusPill } from "./StatusPill";
import { GoalsPanel } from "./GoalsPanel";
import { BeadsPanel } from "./BeadsPanel";
import { SchedulePanel } from "../schedule/SchedulePanel";
import { RuntimePanel } from "./RuntimePanel";
import { SetupWizard } from "../setup/SetupWizard";
import { runtimeStatusQuery } from "../lib/queries";

// Consolidated nav (heavy grouping): four rail surfaces, each grouped one
// fanning out to sub-views via an in-surface segmented control.
// Core surfaces are the fixed literals; plugin views (ADR 0026) add dynamic
// surfaces keyed `plugin:<pluginId>:<viewId>`. The `(string & {})` keeps literal
// autocomplete while allowing those runtime keys.
type Surface = "chat" | "activity" | "studio" | "knowledge" | "system" | "settings" | (string & {});

// Lucide icon names a plugin view may use for its rail glyph (ADR 0026, PR1 set;
// PR2 widens the allowlist). Unknown/missing → a generic plugin glyph.
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
function pluginViewIcon(name?: string): ReactNode {
  const Icon = (name && PLUGIN_VIEW_ICONS[name]) || Puzzle;
  return <Icon size={18} />;
}
// Studio = the workflow authoring/inspection surface. Per ADR 0020 execution is
// a chat gesture (run subagents/workflows via /<name>), not a surface — so the
// old "Run" tab is gone and Studio is just Workflows.
type SystemTab = "runtime" | "telemetry";
// Activity = the "triggers / events" surface (ADR 0009): what happened (thread),
// inbound (inbox), and timed (schedule — cron is a trigger, not a work-type).
type ActivityTab = "thread" | "inbox" | "schedule";
// Knowledge = what the agent knows (ADR 0020): the searchable knowledge Store
// (factual memory) + Playbooks (procedural memory). Store leads.
type KnowledgeTab = "store" | "playbooks";
// The agent's persistent working memory, grouped in the right sidebar:
// its notebook, its task board, and its goals.
type RightPanel = "notes" | "beads" | "goals";

function createNoteTab() {
  const now = Date.now();
  const id = `note-${now}-${Math.random().toString(36).slice(2, 8)}`;
  return {
    id,
    name: "Notes",
    content: "",
    permissions: { agentRead: true, agentWrite: true },
    metadata: {
      createdAt: now,
      updatedAt: now,
      wordCount: 0,
      characterCount: 0,
    },
  };
}

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
  const [surface, setSurface] = useState<Surface>("chat");
  // Background-streaming indicator for the Chat rail (narrow selector → only
  // re-renders when the boolean flips, not per token).
  const chatStreaming = useAnyChatStreaming();
  const [systemTab, setSystemTab] = useState<SystemTab>("runtime");
  const [activityTab, setActivityTab] = useState<ActivityTab>("thread");
  const [knowledgeTab, setKnowledgeTab] = useState<KnowledgeTab>("store");
  const [rightPanel, setRightPanel] = useState<RightPanel>("notes");
  // Collapsible/resizable right panel (persisted). Flag is "1"/"" string; width
  // is a px string clamped on read.
  const [rightCollapsed, setRightCollapsed] = useLocalStorageState("protoagent.rightCollapsed", "");
  const [rightWidthStr, setRightWidthStr] = useLocalStorageState("protoagent.rightWidth", "360");
  const rightWidth = Math.min(720, Math.max(280, parseInt(rightWidthStr, 10) || 360));
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
  const pluginRail = (runtime?.plugins ?? [])
    .filter((p) => p.enabled && p.views?.length)
    .flatMap((p) => (p.views ?? []).map((v) => ({ ...v, key: `plugin:${p.id}:${v.id}` })));
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
  const [workspace, setWorkspace] = useState<NotesWorkspace | null>(null);
  const [error, setError] = useState("");

  const [notesBusy, setNotesBusy] = useState(false);
  const [notesDirty, setNotesDirty] = useState(false);



  const activeTab = workspace?.tabs[workspace.activeTabId] || null;
  const canUndoNote = Boolean(
    ((activeTab?.metadata as Record<string, unknown> | undefined)?.history as unknown[] | undefined)?.length,
  );

  // Notes are agent-global (one persistent store). Beads/Goals/runtime own their
  // data via TanStack Query (ADR 0013); this only loads the notes workspace.
  async function refreshProjectState() {
    try {
      const notesPayload = await api.getNotes();
      setWorkspace(notesPayload.workspace);
      setNotesDirty(false);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  // Load notes once on mount (runtime loads via its own query).
  useEffect(() => {
    void refreshProjectState();
  }, []);

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

  useEffect(() => {
    if (!notesDirty || !workspace) return;
    const handle = window.setTimeout(() => {
      void saveWorkspaceSnapshot(workspace, { quiet: true });
    }, 800);
    return () => window.clearTimeout(handle);
  }, [notesBusy, notesDirty, workspace]);

  // Live notes refresh — the agent (via notes_write) or another tab can change
  // the workspace on disk. Poll while the Notes panel is open and adopt newer
  // server state, but never clobber the user's unsaved edits (notesDirty) and
  // keep their active tab selection.
  useEffect(() => {
    if (rightPanel !== "notes") return;
    const handle = window.setInterval(async () => {
      if (notesDirty || notesBusy) return;
      try {
        const { workspace: latest } = await api.getNotes();
        setWorkspace((current) => {
          if (!current || latest.workspaceVersion <= current.workspaceVersion) return current;
          const keepActive = latest.tabs[current.activeTabId] ? current.activeTabId : latest.activeTabId;
          return { ...latest, activeTabId: keepActive };
        });
      } catch {
        /* transient — retry next tick */
      }
    }, 4000);
    return () => window.clearInterval(handle);
  }, [rightPanel, notesDirty, notesBusy]);


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
  useEffect(() => {
    if (viewingInbox()) setInboxUnread(0);
  }, [surface, activityTab]);

  function updateWorkspace(nextWorkspace: NotesWorkspace) {
    setWorkspace(nextWorkspace);
    setNotesDirty(true);
  }

  // Undo the last write to the active tab, restoring the previous version from
  // the per-tab history that notes_write / the editor record.
  function undoActiveNote() {
    if (!workspace || !activeTab) return;
    const meta = (activeTab.metadata || {}) as Record<string, unknown>;
    const history = (meta.history as Array<{ content: string }> | undefined) || [];
    if (!history.length) return;
    const restored = history[history.length - 1].content;
    updateWorkspace({
      ...workspace,
      workspaceVersion: workspace.workspaceVersion + 1,
      tabs: {
        ...workspace.tabs,
        [activeTab.id]: {
          ...activeTab,
          content: restored,
          metadata: {
            ...meta,
            history: history.slice(0, -1),
            updatedAt: Date.now(),
            characterCount: restored.length,
            wordCount: restored.split(/\s+/).filter(Boolean).length,
          },
        },
      },
    });
  }

  function saveActiveNote(content: string) {
    if (!workspace || !activeTab) return;
    const nextWorkspace: NotesWorkspace = {
      ...workspace,
      workspaceVersion: workspace.workspaceVersion + 1,
      tabs: {
        ...workspace.tabs,
        [activeTab.id]: {
          ...activeTab,
          content,
          metadata: {
            ...activeTab.metadata,
            updatedAt: Date.now(),
            characterCount: content.length,
            wordCount: content.trim() ? content.trim().split(/\s+/).length : 0,
          },
        },
      },
    };
    updateWorkspace(nextWorkspace);
  }

  async function saveWorkspaceSnapshot(
    snapshot: NotesWorkspace,
    options: { quiet?: boolean } = {},
  ) {
    if (notesBusy) return;
    setNotesBusy(true);
    if (!options.quiet) setError("");
    try {
      await api.saveNotes(snapshot);
      setNotesDirty(false);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setNotesBusy(false);
    }
  }

  async function persistNotes() {
    if (!workspace) return;
    await saveWorkspaceSnapshot(workspace);
  }

  function createNote() {
    if (!workspace) return;
    const tab = createNoteTab();
    updateWorkspace({
      ...workspace,
      workspaceVersion: workspace.workspaceVersion + 1,
      activeTabId: tab.id,
      tabOrder: [...workspace.tabOrder, tab.id],
      tabs: { ...workspace.tabs, [tab.id]: tab },
    });
  }

  function deleteActiveNote() {
    if (!workspace || workspace.tabOrder.length <= 1) return;
    const name = workspace.tabs[workspace.activeTabId]?.name || "this note";
    setConfirmState({
      title: "Delete this note?",
      message: `"${name}" will be removed from the workspace.`,
      confirmLabel: "Delete note",
      onConfirm: doDeleteActiveNote,
    });
  }

  function doDeleteActiveNote() {
    if (!workspace || workspace.tabOrder.length <= 1) return;
    const nextOrder = workspace.tabOrder.filter((id) => id !== workspace.activeTabId);
    const nextTabs = { ...workspace.tabs };
    delete nextTabs[workspace.activeTabId];
    updateWorkspace({
      ...workspace,
      workspaceVersion: workspace.workspaceVersion + 1,
      activeTabId: nextOrder[0],
      tabOrder: nextOrder,
      tabs: nextTabs,
    });
  }

  function renameActiveNote(name: string) {
    if (!workspace || !activeTab) return;
    updateWorkspace({
      ...workspace,
      workspaceVersion: workspace.workspaceVersion + 1,
      tabs: {
        ...workspace.tabs,
        [activeTab.id]: {
          ...activeTab,
          name,
          metadata: { ...activeTab.metadata, updatedAt: Date.now() },
        },
      },
    });
  }

  function toggleActiveNotePermission(permission: "agentRead" | "agentWrite", value: boolean) {
    if (!workspace || !activeTab) return;
    updateWorkspace({
      ...workspace,
      workspaceVersion: workspace.workspaceVersion + 1,
      tabs: {
        ...workspace.tabs,
        [activeTab.id]: {
          ...activeTab,
          permissions: { ...activeTab.permissions, [permission]: value },
          metadata: { ...activeTab.metadata, updatedAt: Date.now() },
        },
      },
    });
  }

  // Drag the right panel's left edge to resize (clamped 280–720px, persisted).
  function startRightResize(e: React.MouseEvent) {
    e.preventDefault();
    const startX = e.clientX;
    const startW = rightWidth;
    const onMove = (ev: MouseEvent) => {
      const next = Math.min(720, Math.max(280, startW + (startX - ev.clientX)));
      setRightWidthStr(String(Math.round(next)));
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

  return (
    <div className={`app-shell${isTauriMac ? " is-tauri-mac" : ""}`}>
      <IntroSplash />
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
              void refreshProjectState();
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

      <div
        className={`workspace ${rightCollapsed ? "right-collapsed" : ""}`}
        style={{ "--right-width": rightCol } as CSSProperties}
      >
        <aside className="rail" aria-label="Workspace surfaces">
          <RailButton
            active={surface === "chat"}
            label="Chat"
            icon={<MessageSquare size={18} />}
            onClick={() => setSurface("chat")}
            dot={chatStreaming && surface !== "chat"}
          />
          <RailButton
            active={surface === "activity"}
            label="Activity"
            icon={<Activity size={18} />}
            onClick={() => setSurface("activity")}
            badge={activityUnread + inboxUnread}
          />
          <RailButton
            active={surface === "studio"}
            label="Studio"
            icon={<Boxes size={18} />}
            onClick={() => setSurface("studio")}
          />
          <RailButton
            active={surface === "knowledge"}
            label="Knowledge"
            icon={<BookMarked size={18} />}
            onClick={() => setSurface("knowledge")}
          />
          <RailButton
            active={surface === "system"}
            label="System"
            icon={<Gauge size={18} />}
            onClick={() => setSurface("system")}
          />
          <RailButton
            active={surface === "settings"}
            label="Settings"
            icon={<Settings2 size={18} />}
            onClick={() => setSurface("settings")}
          />
          {/* Plugin-contributed rail surfaces (ADR 0026) — dynamic, from runtime-status. */}
          {pluginRail.map((v) => (
            <RailButton
              key={v.key}
              active={surface === v.key}
              label={v.label}
              icon={pluginViewIcon(v.icon)}
              onClick={() => setSurface(v.key)}
            />
          ))}
        </aside>

        <main className="stage">
          {error ? (
            <div className="error-strip" role="alert">
              <CircleAlert size={16} />
              <span>{error}</span>
            </div>
          ) : null}

          {/* In-surface sub-nav for the grouped rail surfaces. */}
          {surface === "activity" ? (
            <div className="stage-subnav">
              <button className={activityTab === "thread" ? "active" : ""} onClick={() => setActivityTab("thread")}>
                <Activity size={15} /> Thread
              </button>
              <button className={activityTab === "inbox" ? "active" : ""} onClick={() => setActivityTab("inbox")}>
                <Inbox size={15} /> Inbox
                {inboxUnread ? <span className="subnav-badge" data-testid="inbox-badge">{inboxUnread > 9 ? "9+" : inboxUnread}</span> : null}
              </button>
              <button className={activityTab === "schedule" ? "active" : ""} onClick={() => setActivityTab("schedule")}>
                <CalendarClock size={15} /> Schedule
              </button>
            </div>
          ) : null}
          {surface === "knowledge" ? (
            // Store (factual memory) + Playbooks (procedural memory) — ADR 0020.
            <div className="stage-subnav">
              <button className={knowledgeTab === "store" ? "active" : ""} onClick={() => setKnowledgeTab("store")}>
                <Database size={15} /> Store
              </button>
              <button className={knowledgeTab === "playbooks" ? "active" : ""} onClick={() => setKnowledgeTab("playbooks")}>
                <BookMarked size={15} /> Skills
              </button>
            </div>
          ) : null}
          {surface === "system" ? (
            <div className="stage-subnav">
              <button className={systemTab === "runtime" ? "active" : ""} onClick={() => setSystemTab("runtime")}>
                <Gauge size={15} /> Runtime
              </button>
              <button className={systemTab === "telemetry" ? "active" : ""} onClick={() => setSystemTab("telemetry")}>
                <BarChart3 size={15} /> Telemetry
              </button>
            </div>
          ) : null}

          {/* ChatSurface is rendered UNCONDITIONALLY and hidden via `active` when
              off-tab — so an in-flight turn keeps streaming in the background and
              the chat is progressing when you navigate back (not torn down). */}
          <ChatSurface onError={setError} active={surface === "chat"} />

          {surface === "studio" ? <WorkflowsSurface /> : null}

          {surface === "activity" && activityTab === "thread" ? <ActivitySurface onError={setError} /> : null}
          {surface === "activity" && activityTab === "inbox" ? <InboxPanel /> : null}

          {surface === "activity" && activityTab === "schedule" ? <SchedulePanel /> : null}

          {surface === "system" && systemTab === "runtime" ? <RuntimePanel /> : null}

          {surface === "system" && systemTab === "telemetry" ? <TelemetrySurface /> : null}
          {surface === "knowledge" && knowledgeTab === "store" ? <KnowledgeStore onError={setError} /> : null}
          {surface === "knowledge" && knowledgeTab === "playbooks" ? <PlaybooksSurface onError={setError} /> : null}
          {surface === "settings" ? <SettingsSurface /> : null}

          {/* Plugin view (ADR 0026) — the plugin serves the page; PluginView hosts
              it in a same-origin iframe. ChatSurface stays mounted above (hidden)
              so chat continuity holds while a plugin view is open. Keyed so
              switching views resets the iframe's load state. */}
          {activePluginView ? (
            <PluginView key={activePluginView.key} view={activePluginView} />
          ) : null}
        </main>

        <aside className="right-panel">
          {!rightCollapsed ? (
            <div
              className="resize-handle"
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize side panel"
              onMouseDown={startRightResize}
              data-testid="right-resize"
            />
          ) : null}
          <div className="segmented">
            <button type="button" className={rightPanel === "notes" ? "active" : ""} onClick={() => setRightPanel("notes")}>
              <FileText size={15} />
              Notes
            </button>
            <button type="button" className={rightPanel === "beads" ? "active" : ""} onClick={() => setRightPanel("beads")}>
              <Boxes size={15} />
              Beads
            </button>
            <button type="button" className={rightPanel === "goals" ? "active" : ""} onClick={() => setRightPanel("goals")}>
              <Target size={15} />
              Goals
            </button>
          </div>

          {rightPanel === "notes" ? (
            <section className="panel side-panel notes-panel">
              <div className="panel-header compact">
                <div>
                  <h2>{activeTab?.name || "Notes"}</h2>
                  <p className="panel-kicker">
                    {workspace ? `${workspace.tabOrder.length} tab${workspace.tabOrder.length === 1 ? "" : "s"}${notesDirty ? " • unsaved" : ""}` : "not loaded"}
                  </p>
                </div>
                <div className="notes-actions">
                  <button className="icon-button" type="button" onClick={createNote} disabled={!workspace} title="New note">
                    <Plus size={16} />
                  </button>
                  <button className="icon-button" type="button" onClick={deleteActiveNote} disabled={!workspace || workspace.tabOrder.length <= 1} title="Delete note">
                    <Trash2 size={16} />
                  </button>
                  <button className="icon-button" type="button" onClick={undoActiveNote} disabled={!canUndoNote} title="Undo last change">
                    <Undo2 size={16} />
                  </button>
                  <button className="icon-button" type="button" onClick={() => void persistNotes()} disabled={!workspace || notesBusy} title="Save notes">
                    {notesBusy ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
                  </button>
                </div>
              </div>
              {workspace ? (
                <div className="notes-tabbar">
                  {workspace.tabOrder.map((tabId) => {
                    const tab = workspace.tabs[tabId];
                    if (!tab) return null;
                    const active = tab.id === workspace.activeTabId;
                    return (
                      <button className={active ? "active" : ""} type="button" key={tab.id} onClick={() => updateWorkspace({ ...workspace, activeTabId: tab.id })}>
                        {tab.name || "Notes"}
                      </button>
                    );
                  })}
                </div>
              ) : null}
              {activeTab ? (
                <div className="notes-meta">
                  <input
                    value={activeTab.name}
                    onChange={(event) => renameActiveNote(event.target.value)}
                    aria-label="Note name"
                  />
                  <label className="checkbox-field">
                    <input
                      type="checkbox"
                      checked={activeTab.permissions.agentRead}
                      onChange={(event) => toggleActiveNotePermission("agentRead", event.target.checked)}
                    />
                    <span>Agent read</span>
                  </label>
                  <label className="checkbox-field">
                    <input
                      type="checkbox"
                      checked={activeTab.permissions.agentWrite}
                      onChange={(event) => toggleActiveNotePermission("agentWrite", event.target.checked)}
                    />
                    <span>Agent write</span>
                  </label>
                </div>
              ) : null}
              <textarea
                className="notes-editor"
                value={activeTab?.content || ""}
                onChange={(event) => saveActiveNote(event.target.value)}
                placeholder="Project notes"
                disabled={!workspace}
              />
            </section>
          ) : null}

          {rightPanel === "beads" ? <BeadsPanel confirm={setConfirmState} /> : null}

          {rightPanel === "goals" ? <GoalsPanel /> : null}
        </aside>
      </div>

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
          onClick={() => setRightCollapsed(rightCollapsed ? "" : "1")}
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
          void refreshProjectState();
        }}
      />

      <ConfirmDialog
        open={confirmState !== null}
        title={confirmState?.title ?? ""}
        message={confirmState?.message}
        confirmLabel={confirmState?.confirmLabel}
        onConfirm={() => {
          confirmState?.onConfirm();
          setConfirmState(null);
        }}
        onCancel={() => setConfirmState(null)}
      />
    </div>
  );
}

function RailButton({
  active,
  label,
  icon,
  onClick,
  badge,
  dot,
}: {
  active: boolean;
  label: string;
  icon: ReactNode;
  onClick: () => void;
  badge?: number;
  // A small pulsing indicator (no count) — e.g. a chat turn streaming in the
  // background while you're on another tab.
  dot?: boolean;
}) {
  return (
    <button className={active ? "active" : ""} type="button" onClick={onClick} title={label} aria-label={label}>
      {icon}
      <span>{label}</span>
      {badge ? (
        <span className="rail-badge" data-testid="activity-badge">
          {badge > 9 ? "9+" : badge}
        </span>
      ) : dot ? (
        <span className="rail-dot" data-testid="chat-streaming-dot" aria-label="streaming" />
      ) : null}
    </button>
  );
}

