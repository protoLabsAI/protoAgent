import { QueryErrorResetBoundary, useMutation, useSuspenseQuery } from "@tanstack/react-query";
import { Loader2, Play, Plus, Trash2 } from "lucide-react";
import { Suspense, useState } from "react";

import { api } from "../lib/api";
import { subagentsQuery } from "../lib/queries";
import { ErrorBoundary, PanelError, PanelSkeleton } from "./ErrorBoundary";
import { StatusPill } from "./StatusPill";

// Studio → Run: launch one subagent (or a batch) manually. On the TanStack
// Query data layer (ADR 0013): the subagent registry is a useSuspenseQuery; the
// run is a useMutation. Form state is local.

type BatchTask = { id: string; type: string; description: string; prompt: string };

const sessionId = "operator-default";

function createBatchTask(type = "researcher"): BatchTask {
  return {
    id: `batch-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    type,
    description: "",
    prompt: "",
  };
}

function RunBody() {
  const { data } = useSuspenseQuery(subagentsQuery());
  const subagents = data.subagents;

  const [mode, setMode] = useState<"single" | "batch">("single");
  const [type, setType] = useState("researcher");
  const [description, setDescription] = useState("");
  const [prompt, setPrompt] = useState("");
  const [batchTasks, setBatchTasks] = useState<BatchTask[]>(() => [createBatchTask()]);
  const [emitSkill, setEmitSkill] = useState(false);
  const [output, setOutput] = useState("");

  // Default the type to the first registered subagent if the current pick isn't
  // one (e.g. the registry changed under us).
  const typeValue = subagents.some((s) => s.name === type) ? type : subagents[0]?.name || "researcher";

  const run = useMutation({
    mutationFn: () => {
      if (mode === "single") {
        return api.runSubagent({
          session_id: sessionId,
          type: typeValue,
          description: description.trim(),
          prompt: prompt.trim(),
          emit_skill: emitSkill,
        });
      }
      const tasks = batchTasks
        .filter((t) => t.prompt.trim())
        .map((t) => ({
          type: t.type,
          description: t.description.trim(),
          prompt: t.prompt.trim(),
          emit_skill: emitSkill,
        }));
      return api.runSubagentBatch({ session_id: sessionId, tasks });
    },
    onMutate: () => setOutput(""),
    onSuccess: (r) => setOutput(r.output),
  });

  const updateBatchTask = (id: string, patch: Partial<BatchTask>) =>
    setBatchTasks((tasks) => tasks.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  const addBatchTask = () => setBatchTasks((tasks) => [...tasks, createBatchTask(typeValue)]);
  const removeBatchTask = (id: string) =>
    setBatchTasks((tasks) => (tasks.length > 1 ? tasks.filter((t) => t.id !== id) : tasks));

  const canRun = mode === "single" ? Boolean(prompt.trim()) : batchTasks.some((t) => t.prompt.trim());

  return (
    <>
      <div className="panel-header">
        <div>
          <h1>Run</h1>
          <p className="panel-kicker">one focused worker, now · {subagents.length} subagent type{subagents.length === 1 ? "" : "s"}</p>
        </div>
        <StatusPill label={run.isPending ? "running" : "ready"} tone={run.isPending ? "warning" : "muted"} />
      </div>
      <div className="stage-body">
        <div className="subagent-mode segmented">
          <button type="button" className={mode === "single" ? "active" : ""} onClick={() => setMode("single")}>
            Single
          </button>
          <button type="button" className={mode === "batch" ? "active" : ""} onClick={() => setMode("batch")}>
            Batch
          </button>
        </div>
        <div className="subagent-grid">
          <label className="field">
            <span>Type</span>
            <select value={typeValue} onChange={(event) => setType(event.target.value)}>
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
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="Short task label"
            />
          </label>
          <label className="checkbox-field">
            <input type="checkbox" checked={emitSkill} onChange={(event) => setEmitSkill(event.target.checked)} />
            <span>Emit skill</span>
          </label>
        </div>
        {mode === "single" ? (
          <label className="field grow">
            <span>Prompt</span>
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
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
          {mode === "batch" ? (
            <button className="secondary-button" type="button" onClick={addBatchTask}>
              <Plus size={15} />
              Add task
            </button>
          ) : null}
          <button
            className="primary-button"
            type="button"
            onClick={() => run.mutate()}
            disabled={run.isPending || !canRun}
          >
            {run.isPending ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
            {mode === "single" ? "Run" : "Run batch"}
          </button>
        </div>
        {run.isError ? <p className="workflow-failed">{(run.error as Error).message}</p> : null}
        {output ? <pre className="output-block">{output}</pre> : null}
      </div>
    </>
  );
}

export function RunPanel() {
  return (
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="subagents" />}>
            <Suspense fallback={<PanelSkeleton label="Loading subagents…" />}>
              <RunBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
