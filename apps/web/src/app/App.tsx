import {
  Activity,
  BarChart3,
  BookMarked,
  BookOpen,
  Bot,
  Boxes,
  CalendarClock,
  CircleAlert,
  Database,
  FileText,
  Gauge,
  Github,
  Inbox,
  Loader2,
  MessageSquare,
  PanelRight,
  Play,
  Plus,
  Save,
  Settings2,
  Sparkles,
  Target,
  Undo2,
  Trash2,
  Workflow,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { IntroSplash } from "./IntroSplash";

import { ActivitySurface } from "../activity/ActivitySurface";
import { ConfirmDialog } from "./ConfirmDialog";
import { InboxPanel } from "../inbox/InboxPanel";
import { ChatSurface } from "../chat/ChatSurface";
import { PlaybooksSurface } from "../playbooks/PlaybooksSurface";
import { SettingsSurface } from "../settings/SettingsSurface";
import { TelemetrySurface } from "../telemetry/TelemetrySurface";
import { WorkflowsSurface } from "../workflows/WorkflowsSurface";
import { api } from "../lib/api";
import { onConnectionChange, onServerEvent } from "../lib/events";
import type { NotesWorkspace, RuntimeStatus, Subagent } from "../lib/types";
import { StatusPill } from "./StatusPill";
import { GoalsPanel } from "./GoalsPanel";
import { BeadsPanel } from "./BeadsPanel";
import { SchedulePanel } from "../schedule/SchedulePanel";
import { SetupWizard } from "../setup/SetupWizard";

// Consolidated nav (heavy grouping): four rail surfaces, each grouped one
// fanning out to sub-views via an in-surface segmented control.
type Surface = "chat" | "activity" | "studio" | "knowledge" | "system";
// Studio = the "make the agent do work" surface, ordered by altitude
// (ADR 0009): goals (autonomy) → workflows (orchestration) → run (execution).
type StudioTab = "workflows" | "run";
type SystemTab = "runtime" | "telemetry" | "settings";
// Activity = the "triggers / events" surface (ADR 0009): what happened (thread),
// inbound (inbox), and timed (schedule — cron is a trigger, not a work-type).
type ActivityTab = "thread" | "inbox" | "schedule";
// The agent's persistent working memory, grouped in the right sidebar:
// its notebook, its task board, and its goals.
type RightPanel = "notes" | "beads" | "goals";
type SubagentMode = "single" | "batch";

type BatchTask = {
  id: string;
  type: string;
  description: string;
  prompt: string;
};

const sessionId = "operator-default";

function createBatchTask(type = "researcher"): BatchTask {
  return {
    id: `batch-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    type,
    description: "",
    prompt: "",
  };
}

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

function formatBool(value: boolean) {
  return value ? "on" : "off";
}

function statusTone(ok?: boolean) {
  if (ok === undefined) return "muted";
  return ok ? "success" : "error";
}

export function App() {
  const [surface, setSurface] = useState<Surface>("chat");
  const [studioTab, setStudioTab] = useState<StudioTab>("workflows");
  const [systemTab, setSystemTab] = useState<SystemTab>("runtime");
  const [activityTab, setActivityTab] = useState<ActivityTab>("thread");
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
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [subagents, setSubagents] = useState<Subagent[]>([]);
  const [workspace, setWorkspace] = useState<NotesWorkspace | null>(null);
  const [status, setStatus] = useState("ready");
  const [error, setError] = useState("");

  const [subagentType, setSubagentType] = useState("researcher");
  const [subagentMode, setSubagentMode] = useState<SubagentMode>("single");
  const [subagentDescription, setSubagentDescription] = useState("");
  const [subagentPrompt, setSubagentPrompt] = useState("");
  const [batchTasks, setBatchTasks] = useState<BatchTask[]>(() => [createBatchTask()]);
  const [emitSkill, setEmitSkill] = useState(false);
  const [subagentOutput, setSubagentOutput] = useState("");
  const [subagentBusy, setSubagentBusy] = useState(false);

  const [notesBusy, setNotesBusy] = useState(false);
  const [notesDirty, setNotesDirty] = useState(false);



  const activeTab = workspace?.tabs[workspace.activeTabId] || null;
  const canUndoNote = Boolean(
    ((activeTab?.metadata as Record<string, unknown> | undefined)?.history as unknown[] | undefined)?.length,
  );

  async function refreshRuntime() {
    const [runtimePayload, subagentPayload] = await Promise.all([
      api.runtimeStatus(),
      api.subagents(),
    ]);
    setRuntime(runtimePayload);
    setSubagents(subagentPayload.subagents);
    if (!subagentPayload.subagents.some((item) => item.name === subagentType)) {
      setSubagentType(subagentPayload.subagents[0]?.name || "researcher");
    }
    return runtimePayload;
  }

  // Notes are agent-global (one persistent store). Beads + Goals own their data
  // via TanStack Query inside their panels (ADR 0013), so this only loads notes.
  async function refreshProjectState() {
    const notesPayload = await api.getNotes();
    setWorkspace(notesPayload.workspace);
    setNotesDirty(false);
  }

  async function refreshAll() {
    setStatus("refreshing");
    setError("");
    try {
      const runtimePayload = await refreshRuntime();
      // Adopt the server's default project as the fs working dir if none is
      // set (it seeds the setup wizard's allowed-dirs); notes/beads load
      // globally regardless.
      if (!projectPath.trim() && runtimePayload.project.path) {
        setProjectPath(runtimePayload.project.path);
      }
      await refreshProjectState();
      setStatus("ready");
    } catch (exc) {
      setStatus("error");
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      // The desktop app launches the server as a bundled sidecar, which can
      // take a few seconds to boot. Probe with backoff before the first load
      // so the startup gap doesn't surface as an error. In browser mode the
      // server is already up, so the first probe succeeds immediately.
      for (let attempt = 0; attempt < 30 && !cancelled; attempt += 1) {
        try {
          await api.runtimeStatus();
          break;
        } catch {
          if (attempt === 0) setStatus("starting server…");
          await new Promise((resolve) => window.setTimeout(resolve, 1000));
        }
      }
      if (!cancelled) void refreshAll();
    })();
    return () => {
      cancelled = true;
    };
  }, []);

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

  async function runSubagent() {
    const prompt = subagentPrompt.trim();
    const runnableBatchTasks = batchTasks.filter((task) => task.prompt.trim());
    if (subagentBusy) return;
    if (subagentMode === "single" && !prompt) return;
    if (subagentMode === "batch" && runnableBatchTasks.length === 0) return;
    setSubagentBusy(true);
    setError("");
    setSubagentOutput("");
    try {
      const response = subagentMode === "single"
        ? await api.runSubagent({
            session_id: sessionId,
            type: subagentType,
            description: subagentDescription.trim(),
            prompt,
            emit_skill: emitSkill,
          })
        : await api.runSubagentBatch({
            session_id: sessionId,
            tasks: runnableBatchTasks.map((task) => ({
              type: task.type,
              description: task.description.trim(),
              prompt: task.prompt.trim(),
              emit_skill: emitSkill,
            })),
          });
      setSubagentOutput(response.output);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSubagentBusy(false);
    }
  }

  function updateBatchTask(id: string, patch: Partial<BatchTask>) {
    setBatchTasks((tasks) => tasks.map((task) => (task.id === id ? { ...task, ...patch } : task)));
  }

  function addBatchTask() {
    setBatchTasks((tasks) => [...tasks, createBatchTask(subagentType)]);
  }

  function removeBatchTask(id: string) {
    setBatchTasks((tasks) => (tasks.length > 1 ? tasks.filter((task) => task.id !== id) : tasks));
  }


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

  const middleware = useMemo(() => {
    if (!runtime) return [];
    return Object.entries(runtime.middleware).sort(([a], [b]) => a.localeCompare(b));
  }, [runtime]);

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
  // System → Runtime). Worst-state wins.
  const health: { tone: "ok" | "warning" | "error"; label: string } =
    runtime && !runtime.setup_complete ? { tone: "warning", label: "setup pending" }
    : runtime && !runtime.graph_loaded ? { tone: "error", label: "graph offline" }
    : status === "error" ? { tone: "error", label: "error" }
    : status === "streaming" || status === "refreshing" || status.includes("…") ? { tone: "warning", label: status }
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
      {/* macOS desktop: the topbar IS the window's drag region (its brand insets
          right of the native traffic lights — see `.is-tauri-mac .topbar`).
          Interactive children (the status dot) stay clickable; harmless on web. */}
      <header className="topbar" data-tauri-drag-region>
        <div className="brand-lockup">
          <img src="/app/protolabs-icon-outline.svg" alt="" className="brand-mark" />
          <div>
            <div className="brand-name">protoAgent</div>
            <div className="brand-subline">protoLabs.studio</div>
          </div>
        </div>
        <div className="topbar-status">
          <button
            type="button"
            className={`status-dot tone-${health.tone}`}
            onClick={() => void refreshAll()}
            title={
              `Setup: ${runtime?.setup_complete ? "complete" : "pending"}\n` +
              `Graph: ${runtime?.graph_loaded ? "loaded" : "offline"}\n` +
              `Event stream: ${live ? "connected" : "offline"}\n` +
              `Status: ${status}` +
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
          {surface === "studio" ? (
            // Orchestration → execution (ADR 0009). Goals (the autonomy layer)
            // moved to the right sidebar alongside the agent's notes + beads.
            <div className="stage-subnav">
              <button className={studioTab === "workflows" ? "active" : ""} onClick={() => setStudioTab("workflows")}>
                <Workflow size={15} /> Workflows
              </button>
              <button className={studioTab === "run" ? "active" : ""} onClick={() => setStudioTab("run")}>
                <Play size={15} /> Run
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
              <button className={systemTab === "settings" ? "active" : ""} onClick={() => setSystemTab("settings")}>
                <Settings2 size={15} /> Settings
              </button>
            </div>
          ) : null}

          {surface === "chat" ? (
            <ChatSurface onError={setError} />
          ) : null}

          {surface === "studio" && studioTab === "run" ? (
            <section className="panel stage-panel">
              <div className="panel-header">
                <div>
                  <h1>Run</h1>
                  <p className="panel-kicker">one focused worker, now · {subagents.length} subagent type{subagents.length === 1 ? "" : "s"}</p>
                </div>
                <StatusPill label={subagentBusy ? "running" : "ready"} tone={subagentBusy ? "warning" : "muted"} />
              </div>
              <div className="stage-body">
              <div className="subagent-mode segmented">
                <button type="button" className={subagentMode === "single" ? "active" : ""} onClick={() => setSubagentMode("single")}>
                  Single
                </button>
                <button type="button" className={subagentMode === "batch" ? "active" : ""} onClick={() => setSubagentMode("batch")}>
                  Batch
                </button>
              </div>
              <div className="subagent-grid">
                <label className="field">
                  <span>Type</span>
                  <select value={subagentType} onChange={(event) => setSubagentType(event.target.value)}>
                    {subagents.map((subagent) => (
                      <option key={subagent.name} value={subagent.name}>
                        {subagent.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Description</span>
                  <input
                    value={subagentDescription}
                    onChange={(event) => setSubagentDescription(event.target.value)}
                    placeholder="Short task label"
                  />
                </label>
                <label className="checkbox-field">
                  <input
                    type="checkbox"
                    checked={emitSkill}
                    onChange={(event) => setEmitSkill(event.target.checked)}
                  />
                  <span>Emit skill</span>
                </label>
              </div>
              {subagentMode === "single" ? (
                <label className="field grow">
                  <span>Prompt</span>
                  <textarea
                    value={subagentPrompt}
                    onChange={(event) => setSubagentPrompt(event.target.value)}
                    placeholder="Subagent instructions"
                    rows={8}
                  />
                </label>
              ) : (
                <div className="batch-task-list">
                  {batchTasks.map((task, index) => (
                    <div className="batch-task-row" key={task.id}>
                      <div className="batch-task-header">
                        <span>Task {index + 1}</span>
                        <button className="icon-button" type="button" onClick={() => removeBatchTask(task.id)} disabled={batchTasks.length === 1} title="Remove task">
                          <Trash2 size={15} />
                        </button>
                      </div>
                      <div className="batch-task-fields">
                        <label className="field">
                          <span>Type</span>
                          <select value={task.type} onChange={(event) => updateBatchTask(task.id, { type: event.target.value })}>
                            {subagents.map((subagent) => (
                              <option key={subagent.name} value={subagent.name}>
                                {subagent.name}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="field">
                          <span>Description</span>
                          <input value={task.description} onChange={(event) => updateBatchTask(task.id, { description: event.target.value })} placeholder="Task label" />
                        </label>
                      </div>
                      <label className="field">
                        <span>Prompt</span>
                        <textarea value={task.prompt} onChange={(event) => updateBatchTask(task.id, { prompt: event.target.value })} rows={4} />
                      </label>
                    </div>
                  ))}
                </div>
              )}
              <div className="panel-actions">
                {subagentMode === "batch" ? (
                  <button className="secondary-button" type="button" onClick={addBatchTask}>
                    <Plus size={15} />
                    Add task
                  </button>
                ) : null}
                <button
                  className="primary-button"
                  type="button"
                  onClick={() => void runSubagent()}
                  disabled={
                    subagentBusy ||
                    (subagentMode === "single" ? !subagentPrompt.trim() : !batchTasks.some((task) => task.prompt.trim()))
                  }
                >
                  {subagentBusy ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
                  {subagentMode === "single" ? "Run" : "Run batch"}
                </button>
              </div>
              {subagentOutput ? <pre className="output-block">{subagentOutput}</pre> : null}
              </div>
            </section>
          ) : null}

          {surface === "studio" && studioTab === "workflows" ? <WorkflowsSurface /> : null}

          {surface === "activity" && activityTab === "thread" ? <ActivitySurface onError={setError} /> : null}
          {surface === "activity" && activityTab === "inbox" ? <InboxPanel /> : null}

          {surface === "activity" && activityTab === "schedule" ? <SchedulePanel /> : null}

          {surface === "system" && systemTab === "runtime" ? (
            <section className="panel stage-panel">
              <div className="panel-header">
                <div>
                  <h1>Runtime</h1>
                  <p className="panel-kicker">{runtime?.model?.name || "model not configured"}</p>
                </div>
                <StatusPill label={runtime?.scheduler.backend || "scheduler"} tone="muted" />
              </div>
              <div className="stage-body">
              <div className="metric-grid">
                <Metric icon={<Bot size={16} />} label="Agent" value={runtime?.identity?.name || "protoagent"} />
                <Metric icon={<Settings2 size={16} />} label="Provider" value={runtime?.model?.provider || "none"} />
                <Metric icon={<Database size={16} />} label="Knowledge" value={runtime?.knowledge.resolved_path || runtime?.knowledge.configured_path || "disabled"} />
                <Metric icon={<Sparkles size={16} />} label="Goal mode" value={formatBool(Boolean(runtime?.goal.enabled))} />
              </div>
              <p className="panel-kicker">Middleware</p>
              <div className="table-list">
                {middleware.map(([name, enabled]) => (
                  <div className="table-row" key={name}>
                    <span>{name}</span>
                    <StatusPill label={formatBool(enabled)} tone={enabled ? "success" : "muted"} />
                  </div>
                ))}
              </div>

              <p className="panel-kicker">Skills</p>
              <div className="table-list">
                <div className="table-row">
                  <span>SKILL.md skills loaded</span>
                  <StatusPill
                    label={`${runtime?.skills?.count ?? 0}`}
                    tone={(runtime?.skills?.count ?? 0) > 0 ? "success" : "muted"}
                  />
                </div>
              </div>

              <p className="panel-kicker">MCP servers</p>
              <div className="table-list">
                {runtime?.mcp?.servers?.length ? (
                  runtime.mcp.servers.map((server) => (
                    <div className="table-row" key={server.name}>
                      <span>{server.name} · {server.transport}</span>
                      <StatusPill label={`${server.tool_count} tool${server.tool_count === 1 ? "" : "s"}`} tone="success" />
                    </div>
                  ))
                ) : (
                  <div className="table-row">
                    <span>no MCP servers</span>
                    <StatusPill label={runtime?.mcp?.enabled ? "enabled" : "off"} tone="muted" />
                  </div>
                )}
              </div>

              <p className="panel-kicker">Plugins</p>
              <div className="table-list">
                {runtime?.plugins?.length ? (
                  runtime.plugins.map((plugin) => (
                    <div className="table-row" key={plugin.id}>
                      <span>
                        {plugin.name}
                        {plugin.loaded && plugin.tools.length ? ` · ${plugin.tools.length} tool${plugin.tools.length === 1 ? "" : "s"}` : ""}
                        {plugin.loaded && plugin.skills ? ` · ${plugin.skills} skill${plugin.skills === 1 ? "" : "s"}` : ""}
                        {plugin.error ? ` · ${plugin.error}` : ""}
                      </span>
                      <StatusPill
                        label={plugin.loaded ? "loaded" : plugin.error ? "error" : plugin.enabled ? "enabled" : "disabled"}
                        tone={plugin.loaded ? "success" : plugin.error ? "error" : "muted"}
                      />
                    </div>
                  ))
                ) : (
                  <div className="table-row">
                    <span>no plugins</span>
                    <StatusPill label="none" tone="muted" />
                  </div>
                )}
              </div>

              <p className="panel-kicker">Subagents</p>
              <div className="subagent-list">
                {subagents.map((subagent) => (
                  <div className="subagent-row" key={subagent.name}>
                    <div>
                      <strong>{subagent.name}</strong>
                      <span>{subagent.tools.join(", ") || "no tools"}</span>
                    </div>
                    <StatusPill label={`${subagent.max_turns} turns`} tone={subagent.enabled ? "success" : "muted"} />
                  </div>
                ))}
              </div>
              </div>
            </section>
          ) : null}

          {surface === "system" && systemTab === "telemetry" ? <TelemetrySurface /> : null}
          {surface === "knowledge" ? <PlaybooksSurface onError={setError} /> : null}
          {surface === "system" && systemTab === "settings" ? <SettingsSurface /> : null}
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
        onFinished={() => void refreshAll()}
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
}: {
  active: boolean;
  label: string;
  icon: ReactNode;
  onClick: () => void;
  badge?: number;
}) {
  return (
    <button className={active ? "active" : ""} type="button" onClick={onClick} title={label} aria-label={label}>
      {icon}
      <span>{label}</span>
      {badge ? (
        <span className="rail-badge" data-testid="activity-badge">
          {badge > 9 ? "9+" : badge}
        </span>
      ) : null}
    </button>
  );
}

function Metric({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="metric">
      {icon}
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
