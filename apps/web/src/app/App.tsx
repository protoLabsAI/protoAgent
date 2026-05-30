import {
  Bot,
  Boxes,
  CalendarClock,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Database,
  FileText,
  Gauge,
  Loader2,
  MessageSquare,
  Network,
  Play,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  Sparkles,
  Target,
  Undo2,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { ChatSurface } from "../chat/ChatSurface";
import { SettingsSurface } from "../settings/SettingsSurface";
import { api } from "../lib/api";
import type { BeadsIssue, GoalState, NotesWorkspace, RuntimeStatus, ScheduledJob, Subagent } from "../lib/types";
import { ScrollArea } from "./ScrollArea";
import { SetupWizard } from "../setup/SetupWizard";

type Surface = "chat" | "subagents" | "runtime" | "schedule" | "goals" | "settings";
type RightPanel = "notes" | "beads";
type SubagentMode = "single" | "batch";
type StatusTone = "success" | "warning" | "error" | "muted";

type BatchTask = {
  id: string;
  type: string;
  description: string;
  prompt: string;
};

type IssueDraft = {
  title: string;
  description: string;
  type: string;
  priority: number;
};

const sessionId = "operator-default";
const emptyIssueDraft: IssueDraft = {
  title: "",
  description: "",
  type: "task",
  priority: 2,
};

const issueStatusOrder = ["in_progress", "open", "blocked", "deferred", "closed"];

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

function issueStatus(issue: BeadsIssue) {
  return issue.status || "open";
}

function issueType(issue: BeadsIssue) {
  return issue.issue_type || issue.type || "task";
}

function issueStatusLabel(status: string) {
  return status.replace(/_/g, " ");
}

function issueStatusTone(status: string): StatusTone {
  if (status === "closed") return "success";
  if (status === "blocked") return "error";
  if (status === "in_progress" || status === "deferred") return "warning";
  return "muted";
}

function priorityLabel(priority: BeadsIssue["priority"]) {
  if (priority === undefined || priority === null || priority === "") return "P-";
  const value = String(priority);
  return value.toUpperCase().startsWith("P") ? value.toUpperCase() : `P${value}`;
}

function parseTimestamp(value?: string | null) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function dayStart(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

function formatTimestamp(value?: string | null) {
  const date = parseTimestamp(value);
  if (!date) return "";

  const now = new Date();
  const dayDelta = Math.round((dayStart(now) - dayStart(date)) / 86_400_000);
  const time = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date);

  if (dayDelta === 0) return `today at ${time}`;
  if (dayDelta === 1) return `yesterday at ${time}`;

  const options: Intl.DateTimeFormatOptions = {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  };
  if (date.getFullYear() !== now.getFullYear()) {
    options.year = "numeric";
  }
  return new Intl.DateTimeFormat(undefined, options).format(date);
}

function issueGroupId(status: string) {
  return `issue-group-${status.replace(/[^a-z0-9_-]/gi, "-")}`;
}

function formatExactTimestamp(value?: string | null) {
  const date = parseTimestamp(value);
  if (!date) return "";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "full",
    timeStyle: "long",
  }).format(date);
}

function groupIssues(issues: BeadsIssue[]) {
  const buckets = new Map<string, BeadsIssue[]>();
  for (const issue of issues) {
    const status = issueStatus(issue);
    const bucket = buckets.get(status);
    if (bucket) {
      bucket.push(issue);
    } else {
      buckets.set(status, [issue]);
    }
  }

  const ordered = issueStatusOrder.filter((status) => buckets.has(status));
  const rest = [...buckets.keys()].filter((status) => !issueStatusOrder.includes(status)).sort();
  return [...ordered, ...rest].map((status) => ({
    status,
    issues: buckets.get(status) || [],
  }));
}

export function App() {
  const [surface, setSurface] = useState<Surface>("chat");
  const [rightPanel, setRightPanel] = useState<RightPanel>("notes");
  const [projectPath, setProjectPath] = useLocalStorageState("protoagent.projectPath", "");
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [subagents, setSubagents] = useState<Subagent[]>([]);
  const [workspace, setWorkspace] = useState<NotesWorkspace | null>(null);
  const [beadsIssues, setBeadsIssues] = useState<BeadsIssue[]>([]);
  const [beadsReady, setBeadsReady] = useState<boolean | null>(null);
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
  const [issueDraft, setIssueDraft] = useState<IssueDraft>(emptyIssueDraft);
  const [beadsBusy, setBeadsBusy] = useState(false);
  const [collapsedIssueGroups, setCollapsedIssueGroups] = useState<Set<string>>(() => new Set(["closed"]));

  const [scheduleJobs, setScheduleJobs] = useState<ScheduledJob[]>([]);
  const [scheduleBackend, setScheduleBackend] = useState("local");
  const [schedulePrompt, setSchedulePrompt] = useState("");
  const [scheduleWhen, setScheduleWhen] = useState("");
  const [scheduleJobId, setScheduleJobId] = useState("");
  const [scheduleBusy, setScheduleBusy] = useState(false);

  const [goalsList, setGoalsList] = useState<GoalState[]>([]);
  const [goalsBusy, setGoalsBusy] = useState(false);

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

  async function refreshSchedules() {
    try {
      const payload = await api.schedules();
      setScheduleJobs(payload.jobs);
      setScheduleBackend(payload.backend);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function createSchedule() {
    const prompt = schedulePrompt.trim();
    const schedule = scheduleWhen.trim();
    if (!prompt || !schedule || scheduleBusy) return;
    setScheduleBusy(true);
    setError("");
    try {
      await api.addSchedule({ prompt, schedule, job_id: scheduleJobId.trim() || undefined });
      setSchedulePrompt("");
      setScheduleWhen("");
      setScheduleJobId("");
      await refreshSchedules();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setScheduleBusy(false);
    }
  }

  async function cancelScheduleJob(jobId: string) {
    if (scheduleBusy) return;
    setScheduleBusy(true);
    try {
      await api.cancelSchedule(jobId);
      await refreshSchedules();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setScheduleBusy(false);
    }
  }

  async function refreshGoals() {
    try {
      const payload = await api.goals();
      setGoalsList(payload.goals);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function clearGoal(sessionId: string) {
    if (goalsBusy) return;
    setGoalsBusy(true);
    try {
      await api.clearGoal(sessionId);
      await refreshGoals();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setGoalsBusy(false);
    }
  }

  async function refreshProjectState(path = projectPath) {
    if (!path.trim()) return;
    const [notesPayload, beadsStatus] = await Promise.all([
      api.getNotes(path),
      api.beadsStatus(path),
    ]);
    setWorkspace(notesPayload.workspace);
    setNotesDirty(false);
    setBeadsReady(beadsStatus.initialized);
    if (beadsStatus.initialized) {
      const issuesPayload = await api.beadsIssues(path);
      setBeadsIssues(issuesPayload.issues);
    } else {
      setBeadsIssues([]);
    }
  }

  async function refreshAll() {
    setStatus("refreshing");
    setError("");
    try {
      const runtimePayload = await refreshRuntime();
      const nextProjectPath = projectPath.trim() || runtimePayload.project.path;
      if (nextProjectPath && nextProjectPath !== projectPath) {
        setProjectPath(nextProjectPath);
      }
      await refreshProjectState(nextProjectPath);
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
    if (!notesDirty || !workspace || !projectPath.trim()) return;
    const handle = window.setTimeout(() => {
      void saveWorkspaceSnapshot(workspace, { quiet: true });
    }, 800);
    return () => window.clearTimeout(handle);
  }, [notesBusy, notesDirty, projectPath, workspace]);

  // Live notes refresh — the agent (via notes_write) or another tab can change
  // the workspace on disk. Poll while the Notes panel is open and adopt newer
  // server state, but never clobber the user's unsaved edits (notesDirty) and
  // keep their active tab selection.
  useEffect(() => {
    if (rightPanel !== "notes" || !projectPath.trim()) return;
    const handle = window.setInterval(async () => {
      if (notesDirty || notesBusy) return;
      try {
        const { workspace: latest } = await api.getNotes(projectPath);
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
  }, [rightPanel, projectPath, notesDirty, notesBusy]);

  useEffect(() => {
    if (surface === "schedule") void refreshSchedules();
    if (surface === "goals") void refreshGoals();
  }, [surface]);

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

  async function loadProject() {
    setNotesBusy(true);
    setBeadsBusy(true);
    setError("");
    try {
      await refreshProjectState();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setNotesBusy(false);
      setBeadsBusy(false);
    }
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
    if (!workspace || !activeTab || !projectPath.trim()) return;
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
    if (!projectPath.trim() || notesBusy) return;
    setNotesBusy(true);
    if (!options.quiet) setError("");
    try {
      await api.saveNotes(projectPath, snapshot);
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

  async function initBeads() {
    if (!projectPath.trim() || beadsBusy) return;
    setBeadsBusy(true);
    setError("");
    try {
      await api.initBeads(projectPath);
      await refreshProjectState();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBeadsBusy(false);
    }
  }

  async function createIssue() {
    const title = issueDraft.title.trim();
    if (!projectPath.trim() || !title || beadsBusy) return;
    setBeadsBusy(true);
    setError("");
    try {
      const response = await api.createIssue(projectPath, {
        title,
        type: issueDraft.type,
        priority: issueDraft.priority,
        description: issueDraft.description.trim() || undefined,
      });
      setBeadsIssues((items) => [response.issue, ...items]);
      setIssueDraft(emptyIssueDraft);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBeadsBusy(false);
    }
  }

  function replaceIssue(issue: BeadsIssue) {
    setBeadsIssues((items) => items.map((item) => (item.id === issue.id ? { ...item, ...issue } : item)));
  }

  function toggleIssueGroup(status: string) {
    setCollapsedIssueGroups((current) => {
      const next = new Set(current);
      if (next.has(status)) {
        next.delete(status);
      } else {
        next.add(status);
      }
      return next;
    });
  }

  async function updateIssueStatus(issue: BeadsIssue, nextStatus: string) {
    if (!projectPath.trim() || beadsBusy) return;
    setBeadsBusy(true);
    setError("");
    try {
      const response = await api.updateIssue(projectPath, issue.id, { status: nextStatus });
      replaceIssue(response.issue.id ? response.issue : { ...issue, status: nextStatus });
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBeadsBusy(false);
    }
  }

  async function closeIssue(issue: BeadsIssue) {
    if (!projectPath.trim() || beadsBusy) return;
    setBeadsBusy(true);
    setError("");
    try {
      const response = await api.closeIssue(projectPath, issue.id);
      replaceIssue(response.issue.id ? response.issue : { ...issue, status: "closed" });
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBeadsBusy(false);
    }
  }

  async function deleteIssue(issue: BeadsIssue) {
    if (!projectPath.trim() || beadsBusy) return;
    if (!window.confirm(`Delete ${issue.id}?`)) return;
    setBeadsBusy(true);
    setError("");
    try {
      await api.deleteIssue(projectPath, issue.id);
      setBeadsIssues((items) => items.filter((item) => item.id !== issue.id));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBeadsBusy(false);
    }
  }

  const middleware = useMemo(() => {
    if (!runtime) return [];
    return Object.entries(runtime.middleware).sort(([a], [b]) => a.localeCompare(b));
  }, [runtime]);

  const groupedIssues = useMemo(() => groupIssues(beadsIssues), [beadsIssues]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <img src="/app/protolabs-icon-outline.svg" alt="" className="brand-mark" />
          <div>
            <div className="brand-name">protoAgent</div>
            <div className="brand-subline">protoLabs.studio</div>
          </div>
        </div>
        <div className="topbar-status">
          <StatusPill
            label={runtime?.setup_complete ? "setup complete" : "setup pending"}
            tone={statusTone(runtime?.setup_complete)}
          />
          <StatusPill
            label={runtime?.graph_loaded ? "graph loaded" : "graph offline"}
            tone={statusTone(runtime?.graph_loaded)}
          />
          <StatusPill label={status} tone={status === "error" ? "error" : "muted"} />
          <button className="icon-button" type="button" onClick={() => void refreshAll()} title="Refresh">
            <RefreshCw size={16} />
          </button>
        </div>
      </header>

      <div className="workspace">
        <aside className="rail" aria-label="Workspace surfaces">
          <RailButton
            active={surface === "chat"}
            label="Chat"
            icon={<MessageSquare size={18} />}
            onClick={() => setSurface("chat")}
          />
          <RailButton
            active={surface === "subagents"}
            label="Subagents"
            icon={<Network size={18} />}
            onClick={() => setSurface("subagents")}
          />
          <RailButton
            active={surface === "schedule"}
            label="Schedule"
            icon={<CalendarClock size={18} />}
            onClick={() => setSurface("schedule")}
          />
          <RailButton
            active={surface === "goals"}
            label="Goals"
            icon={<Target size={18} />}
            onClick={() => setSurface("goals")}
          />
          <RailButton
            active={surface === "runtime"}
            label="Runtime"
            icon={<Gauge size={18} />}
            onClick={() => setSurface("runtime")}
          />
          <RailButton
            active={surface === "settings"}
            label="Settings"
            icon={<Settings2 size={18} />}
            onClick={() => setSurface("settings")}
          />
        </aside>

        <main className="stage">
          {error ? (
            <div className="error-strip" role="alert">
              <CircleAlert size={16} />
              <span>{error}</span>
            </div>
          ) : null}

          {surface === "chat" ? (
            <ChatSurface onError={setError} />
          ) : null}

          {surface === "subagents" ? (
            <section className="panel stage-panel">
              <div className="panel-header">
                <div>
                  <h1>Manual Subagent</h1>
                  <p className="panel-kicker">{subagents.length} registered</p>
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

          {surface === "schedule" ? (
            <section className="panel stage-panel">
              <div className="panel-header">
                <div>
                  <h1>Schedule</h1>
                  <p className="panel-kicker">{scheduleJobs.length} job{scheduleJobs.length === 1 ? "" : "s"} · {scheduleBackend}</p>
                </div>
                <button className="icon-button" type="button" onClick={() => void refreshSchedules()} title="Refresh">
                  {scheduleBusy ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
                </button>
              </div>

              <div className="stage-body">
              <div className="subagent-grid">
                <label className="field">
                  <span>When (cron or ISO datetime)</span>
                  <input
                    value={scheduleWhen}
                    onChange={(event) => setScheduleWhen(event.target.value)}
                    placeholder='e.g. "0 9 * * 1-5"  or  "2026-06-01T15:00:00Z"'
                  />
                </label>
                <label className="field">
                  <span>Job id (optional)</span>
                  <input
                    value={scheduleJobId}
                    onChange={(event) => setScheduleJobId(event.target.value)}
                    placeholder="auto"
                  />
                </label>
                <button
                  className="primary-button"
                  type="button"
                  onClick={() => void createSchedule()}
                  disabled={scheduleBusy || !schedulePrompt.trim() || !scheduleWhen.trim()}
                >
                  <Plus size={16} />
                  Schedule
                </button>
              </div>
              <label className="field grow">
                <span>Prompt (delivered to the agent when it fires)</span>
                <textarea
                  value={schedulePrompt}
                  onChange={(event) => setSchedulePrompt(event.target.value)}
                  placeholder="What the agent should do when this fires"
                  rows={5}
                />
              </label>

              <div className="subagent-list">
                {scheduleJobs.length ? (
                  scheduleJobs.map((job) => (
                    <div className="subagent-row" key={job.id}>
                      <div>
                        <strong>{job.id}</strong>
                        <span>
                          {job.schedule}
                          {job.next_fire ? ` · next ${job.next_fire}` : ""}
                          {" · "}
                          {job.prompt.length > 80 ? `${job.prompt.slice(0, 80)}…` : job.prompt}
                        </span>
                      </div>
                      <button
                        className="icon-button"
                        type="button"
                        onClick={() => void cancelScheduleJob(job.id)}
                        disabled={scheduleBusy}
                        title="Cancel job"
                      >
                        <Trash2 size={16} />
                      </button>
                    </div>
                  ))
                ) : (
                  <div className="subagent-row">
                    <div>
                      <strong>No scheduled jobs</strong>
                      <span>{scheduleBackend !== "local" && scheduleBackend !== "disabled" ? `jobs may be managed remotely by ${scheduleBackend}` : "create one above"}</span>
                    </div>
                  </div>
                )}
              </div>
              </div>
            </section>
          ) : null}

          {surface === "goals" ? (
            <section className="panel stage-panel">
              <div className="panel-header">
                <div>
                  <h1>Goals</h1>
                  <p className="panel-kicker">{goalsList.length} goal{goalsList.length === 1 ? "" : "s"}</p>
                </div>
                <button className="icon-button" type="button" onClick={() => void refreshGoals()} title="Refresh">
                  {goalsBusy ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
                </button>
              </div>
              <div className="stage-body">
                <div className="subagent-list">
                  {goalsList.length ? (
                    goalsList.map((goal) => (
                      <div className="subagent-row" key={goal.session_id}>
                        <div>
                          <strong>{goal.condition || goal.session_id}</strong>
                          <span>
                            {goal.session_id} · {goal.verifier?.type || "llm"} · iter {goal.iteration ?? 0}/{goal.max_iterations ?? 0}
                            {goal.last_reason ? ` · ${goal.last_reason.length > 70 ? `${goal.last_reason.slice(0, 70)}…` : goal.last_reason}` : ""}
                          </span>
                        </div>
                        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                          <StatusPill
                            label={goal.status}
                            tone={
                              goal.status === "achieved"
                                ? "success"
                                : goal.status === "active"
                                  ? "warning"
                                  : goal.status === "unachievable"
                                    ? "error"
                                    : "muted"
                            }
                          />
                          <button
                            className="icon-button"
                            type="button"
                            onClick={() => void clearGoal(goal.session_id)}
                            disabled={goalsBusy}
                            title="Clear goal"
                          >
                            <Trash2 size={16} />
                          </button>
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="subagent-row">
                      <div>
                        <strong>No goals</strong>
                        <span>set one in chat with <code>/goal …</code></span>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </section>
          ) : null}

          {surface === "runtime" ? (
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

          {surface === "settings" ? <SettingsSurface onError={setError} /> : null}
        </main>

        <aside className="right-panel">
          <div className="project-bar">
            <input
              value={projectPath}
              onChange={(event) => setProjectPath(event.target.value)}
              placeholder="Project path"
              list="operator-allowed-dirs"
            />
            <datalist id="operator-allowed-dirs">
              {(runtime?.project.allowed_dirs || []).map((dir) => (
                <option key={dir} value={dir} />
              ))}
            </datalist>
            <button className="icon-button" type="button" onClick={() => void loadProject()} title="Load project">
              {notesBusy || beadsBusy ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
            </button>
          </div>
          <div className="segmented">
            <button type="button" className={rightPanel === "notes" ? "active" : ""} onClick={() => setRightPanel("notes")}>
              <FileText size={15} />
              Notes
            </button>
            <button type="button" className={rightPanel === "beads" ? "active" : ""} onClick={() => setRightPanel("beads")}>
              <Boxes size={15} />
              Beads
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

          {rightPanel === "beads" ? (
            <section className="panel side-panel beads-panel">
              <div className="panel-header compact">
                <div>
                  <h2>Beads</h2>
                  <p className="panel-kicker">
                    {beadsReady === null ? "not checked" : beadsReady ? `${beadsIssues.length} task${beadsIssues.length === 1 ? "" : "s"}` : "not initialized"}
                  </p>
                </div>
                {beadsReady === false ? (
                  <button className="icon-button" type="button" onClick={() => void initBeads()} title="Initialize beads">
                    <CheckCircle2 size={16} />
                  </button>
                ) : null}
              </div>
              <form
                className="issue-create"
                onSubmit={(event) => {
                  event.preventDefault();
                  void createIssue();
                }}
              >
                <input
                  value={issueDraft.title}
                  onChange={(event) => setIssueDraft((draft) => ({ ...draft, title: event.target.value }))}
                  placeholder="New issue title"
                  disabled={!beadsReady}
                />
                <button className="primary-button" type="submit" disabled={!beadsReady || !issueDraft.title.trim() || beadsBusy}>
                  {beadsBusy ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
                  Add
                </button>
                <div className="issue-create-meta">
                  <select
                    value={issueDraft.type}
                    onChange={(event) => setIssueDraft((draft) => ({ ...draft, type: event.target.value }))}
                    disabled={!beadsReady}
                    aria-label="Issue type"
                  >
                    <option value="task">task</option>
                    <option value="bug">bug</option>
                    <option value="feature">feature</option>
                    <option value="chore">chore</option>
                  </select>
                  <select
                    value={issueDraft.priority}
                    onChange={(event) => setIssueDraft((draft) => ({ ...draft, priority: Number(event.target.value) }))}
                    disabled={!beadsReady}
                    aria-label="Issue priority"
                  >
                    <option value={0}>P0</option>
                    <option value={1}>P1</option>
                    <option value={2}>P2</option>
                    <option value={3}>P3</option>
                    <option value={4}>P4</option>
                  </select>
                  <input
                    value={issueDraft.description}
                    onChange={(event) => setIssueDraft((draft) => ({ ...draft, description: event.target.value }))}
                    placeholder="Description"
                    disabled={!beadsReady}
                  />
                </div>
              </form>
              <ScrollArea className="issue-list" ariaLabel="Beads tasks">
                {beadsReady === null ? (
                  <div className="empty-state stacked">
                    <Boxes size={18} />
                    <span>Load a project to check beads.</span>
                  </div>
                ) : beadsReady === false ? (
                  <div className="empty-state stacked">
                    <Boxes size={18} />
                    <span>Beads is not initialized.</span>
                    <button className="secondary-button" type="button" onClick={() => void initBeads()} disabled={beadsBusy}>
                      <CheckCircle2 size={16} />
                      Initialize
                    </button>
                  </div>
                ) : beadsIssues.length === 0 ? (
                  <div className="empty-state stacked">
                    <Boxes size={18} />
                    <span>No beads loaded.</span>
                  </div>
                ) : (
                  groupedIssues.map((group) => {
                    const isGroupCollapsed = collapsedIssueGroups.has(group.status);
                    const groupBodyId = issueGroupId(group.status);
                    return (
                      <section className={`issue-group${isGroupCollapsed ? " collapsed" : ""}`} key={group.status}>
                        <div className="issue-group-header">
                          <button
                            className="issue-group-toggle"
                            type="button"
                            aria-expanded={!isGroupCollapsed}
                            aria-controls={groupBodyId}
                            onClick={() => toggleIssueGroup(group.status)}
                          >
                            {isGroupCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                            <span>{issueStatusLabel(group.status)}</span>
                          </button>
                          <StatusPill label={String(group.issues.length)} tone="muted" />
                        </div>
                        {!isGroupCollapsed ? (
                          <div className="issue-group-body" id={groupBodyId}>
                            {group.issues.map((issue) => {
                              const status = issueStatus(issue);
                              const isClosed = status === "closed";
                              const isActive = status === "in_progress";
                              const createdLabel = formatTimestamp(issue.created_at);
                              const createdTitle = formatExactTimestamp(issue.created_at);
                              return (
                                <div className="issue-row" key={issue.id}>
                                  <div className="issue-main">
                                    <div className="issue-titleline">
                                      <strong>{issue.title}</strong>
                                    </div>
                                    <div className="issue-toolbar">
                                      <div className="issue-badges">
                                        <span>{issue.id}</span>
                                        <span>{issueType(issue)}</span>
                                        <span>{priorityLabel(issue.priority)}</span>
                                        {createdLabel ? (
                                          <span className="issue-time" title={createdTitle ? `Created ${createdTitle}` : "Created"}>
                                            created {createdLabel}
                                          </span>
                                        ) : null}
                                        {issue.assignee ? <span>{issue.assignee}</span> : null}
                                        <StatusPill label={issueStatusLabel(status)} tone={issueStatusTone(status)} />
                                      </div>
                                      <div className="issue-actions">
                                        {!isClosed ? (
                                          <button
                                            className="icon-button"
                                            type="button"
                                            onClick={() => void updateIssueStatus(issue, isActive ? "open" : "in_progress")}
                                            disabled={beadsBusy}
                                            title={isActive ? "Mark open" : "Start issue"}
                                          >
                                            {isActive ? <CircleAlert size={15} /> : <Play size={15} />}
                                          </button>
                                        ) : null}
                                        <button
                                          className="icon-button"
                                          type="button"
                                          onClick={() => void (isClosed ? updateIssueStatus(issue, "open") : closeIssue(issue))}
                                          disabled={beadsBusy}
                                          title={isClosed ? "Reopen issue" : "Close issue"}
                                        >
                                          {isClosed ? <Play size={15} /> : <CheckCircle2 size={15} />}
                                        </button>
                                        <button
                                          className="icon-button danger"
                                          type="button"
                                          onClick={() => void deleteIssue(issue)}
                                          disabled={beadsBusy}
                                          title="Delete issue"
                                        >
                                          <Trash2 size={15} />
                                        </button>
                                      </div>
                                    </div>
                                    {issue.description ? (
                                      <details className="issue-description-block">
                                        <summary>Description</summary>
                                        <p className="issue-description">{issue.description}</p>
                                      </details>
                                    ) : null}
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                        ) : null}
                      </section>
                    );
                  })
                )}
              </ScrollArea>
            </section>
          ) : null}
        </aside>
      </div>
      <SetupWizard
        open={runtime?.setup_complete === false}
        projectPath={projectPath}
        onProjectPathChange={setProjectPath}
        onFinished={() => void refreshAll()}
      />
    </div>
  );
}

function StatusPill({ label, tone }: { label: string; tone: StatusTone }) {
  return <span className={`status-pill ${tone}`}>{label}</span>;
}

function RailButton({
  active,
  label,
  icon,
  onClick,
}: {
  active: boolean;
  label: string;
  icon: ReactNode;
  onClick: () => void;
}) {
  return (
    <button className={active ? "active" : ""} type="button" onClick={onClick} title={label} aria-label={label}>
      {icon}
      <span>{label}</span>
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
