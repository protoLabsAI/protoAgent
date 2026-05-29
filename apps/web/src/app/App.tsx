import {
  Bot,
  Boxes,
  CheckCircle2,
  CircleAlert,
  Database,
  FileText,
  Gauge,
  Loader2,
  MessageSquare,
  Network,
  Play,
  RefreshCw,
  Save,
  Settings2,
  Sparkles,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { ChatSurface } from "../chat/ChatSurface";
import { api } from "../lib/api";
import type { BeadsIssue, NotesWorkspace, RuntimeStatus, Subagent } from "../lib/types";
import { SetupWizard } from "../setup/SetupWizard";

type Surface = "chat" | "subagents" | "runtime";
type RightPanel = "notes" | "beads";

const sessionId = "operator-default";

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
  const [subagentDescription, setSubagentDescription] = useState("");
  const [subagentPrompt, setSubagentPrompt] = useState("");
  const [emitSkill, setEmitSkill] = useState(false);
  const [subagentOutput, setSubagentOutput] = useState("");
  const [subagentBusy, setSubagentBusy] = useState(false);

  const [notesBusy, setNotesBusy] = useState(false);
  const [issueDraft, setIssueDraft] = useState("");
  const [beadsBusy, setBeadsBusy] = useState(false);

  const activeTab = workspace?.tabs[workspace.activeTabId] || null;

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
  }

  async function refreshProjectState(path = projectPath) {
    if (!path.trim()) return;
    const [notesPayload, beadsStatus] = await Promise.all([
      api.getNotes(path),
      api.beadsStatus(path),
    ]);
    setWorkspace(notesPayload.workspace);
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
      await refreshRuntime();
      await refreshProjectState();
      setStatus("ready");
    } catch (exc) {
      setStatus("error");
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  useEffect(() => {
    void refreshAll();
  }, []);

  async function runSubagent() {
    const prompt = subagentPrompt.trim();
    if (!prompt || subagentBusy) return;
    setSubagentBusy(true);
    setError("");
    setSubagentOutput("");
    try {
      const response = await api.runSubagent({
        session_id: sessionId,
        type: subagentType,
        description: subagentDescription.trim(),
        prompt,
        emit_skill: emitSkill,
      });
      setSubagentOutput(response.output);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSubagentBusy(false);
    }
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

  async function saveActiveNote(content: string) {
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
    setWorkspace(nextWorkspace);
  }

  async function persistNotes() {
    if (!workspace || !projectPath.trim() || notesBusy) return;
    setNotesBusy(true);
    setError("");
    try {
      await api.saveNotes(projectPath, workspace);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setNotesBusy(false);
    }
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
    const title = issueDraft.trim();
    if (!projectPath.trim() || !title || beadsBusy) return;
    setBeadsBusy(true);
    setError("");
    try {
      const response = await api.createIssue(projectPath, title);
      setBeadsIssues((items) => [response.issue, ...items]);
      setIssueDraft("");
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
            active={surface === "runtime"}
            label="Runtime"
            icon={<Gauge size={18} />}
            onClick={() => setSurface("runtime")}
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
              <label className="field grow">
                <span>Prompt</span>
                <textarea
                  value={subagentPrompt}
                  onChange={(event) => setSubagentPrompt(event.target.value)}
                  placeholder="Subagent instructions"
                  rows={8}
                />
              </label>
              <div className="panel-actions">
                <button className="primary-button" type="button" onClick={() => void runSubagent()} disabled={!subagentPrompt.trim() || subagentBusy}>
                  {subagentBusy ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
                  Run
                </button>
              </div>
              {subagentOutput ? <pre className="output-block">{subagentOutput}</pre> : null}
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
              <div className="metric-grid">
                <Metric icon={<Bot size={16} />} label="Agent" value={runtime?.identity?.name || "protoagent"} />
                <Metric icon={<Settings2 size={16} />} label="Provider" value={runtime?.model?.provider || "none"} />
                <Metric icon={<Database size={16} />} label="Knowledge" value={runtime?.knowledge.resolved_path || runtime?.knowledge.configured_path || "disabled"} />
                <Metric icon={<Sparkles size={16} />} label="Goal mode" value={formatBool(Boolean(runtime?.goal.enabled))} />
              </div>
              <div className="table-list">
                {middleware.map(([name, enabled]) => (
                  <div className="table-row" key={name}>
                    <span>{name}</span>
                    <StatusPill label={formatBool(enabled)} tone={enabled ? "success" : "muted"} />
                  </div>
                ))}
              </div>
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
            </section>
          ) : null}
        </main>

        <aside className="right-panel">
          <div className="project-bar">
            <input
              value={projectPath}
              onChange={(event) => setProjectPath(event.target.value)}
              placeholder="Project path"
            />
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
            <section className="panel side-panel">
              <div className="panel-header compact">
                <div>
                  <h2>{activeTab?.name || "Notes"}</h2>
                  <p className="panel-kicker">{workspace ? `${workspace.tabOrder.length} tab` : "not loaded"}</p>
                </div>
                <button className="icon-button" type="button" onClick={() => void persistNotes()} disabled={!workspace || notesBusy} title="Save notes">
                  {notesBusy ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
                </button>
              </div>
              <textarea
                className="notes-editor"
                value={activeTab?.content || ""}
                onChange={(event) => void saveActiveNote(event.target.value)}
                placeholder="Project notes"
                disabled={!workspace}
              />
            </section>
          ) : null}

          {rightPanel === "beads" ? (
            <section className="panel side-panel">
              <div className="panel-header compact">
                <div>
                  <h2>Beads</h2>
                  <p className="panel-kicker">{beadsReady === null ? "not checked" : beadsReady ? "initialized" : "not initialized"}</p>
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
                  value={issueDraft}
                  onChange={(event) => setIssueDraft(event.target.value)}
                  placeholder="New issue title"
                  disabled={!beadsReady}
                />
                <button className="primary-button" type="submit" disabled={!beadsReady || !issueDraft.trim() || beadsBusy}>
                  {beadsBusy ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
                  Add
                </button>
              </form>
              <div className="issue-list">
                {beadsIssues.length === 0 ? (
                  <div className="empty-state">
                    <Boxes size={18} />
                    <span>No beads loaded.</span>
                  </div>
                ) : (
                  beadsIssues.map((issue) => (
                    <div className="issue-row" key={issue.id}>
                      <div>
                        <strong>{issue.title}</strong>
                        <span>{issue.id}</span>
                      </div>
                      <StatusPill label={issue.status || "open"} tone={issue.status === "closed" ? "success" : "warning"} />
                    </div>
                  ))
                )}
              </div>
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

function StatusPill({ label, tone }: { label: string; tone: "success" | "warning" | "error" | "muted" }) {
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
