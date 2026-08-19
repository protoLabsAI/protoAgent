import "./workflows.css";

import { DropdownSelect, Input } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import {
  useMutation,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import {
  Check,
  History,
  Loader2,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  Workflow,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";

import { StagePanel } from "../app/ErrorBoundary";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { api } from "../lib/api";
import { ago, errMsg } from "../lib/format";
import {
  queryKeys,
  subagentsQuery,
  workflowRunHistoryQuery,
  workflowRunsQuery,
  workflowsQuery,
} from "../lib/queries";
import type { WorkflowPausedRun, WorkflowRecipe, WorkflowRunResult, WorkflowSummary } from "../lib/types";
import { RunTimeline, computeLanes } from "./RunTimeline";
import { WorkflowBuilder } from "./WorkflowBuilder";

// Operator surface for declarative workflow recipes (ADR 0002), on the TanStack
// Query data layer (ADR 0013). The Studio's two halves:
//   CREATE — recipe picker + the builder (new or EDIT, loaded from /{name}/recipe),
//     with the recipe's parallelism lanes rendered read-only.
//   TEST — Run starts a DETACHED run (POST /{name}/start) and watches its record
//     live in <RunTimeline> (per-step status/outputs, inline gate approval); past
//     runs come back through the History list, inspected in the same timeline.

// One paused run's card: recipe name, the parked step id, its RENDERED prompt (inputs +
// prior outputs already substituted), and Approve / Edit / Reject. Edit swaps the prompt
// for an inline textarea pre-filled with it; Save & run resumes with the edited text.
function PendingGateCard({
  run,
  busy,
  onApprove,
  onReject,
  onEdit,
}: {
  run: WorkflowPausedRun;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
  onEdit: (prompt: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(run.prompt);

  return (
    <div className="workflow-gate-card">
      <div className="workflow-gate-head">
        <Workflow size={14} />
        <strong>{run.recipe_name}</strong>
        <span className="workflow-step-sub">{run.paused_step}</span>
      </div>

      {editing ? (
        <textarea
          className="workflow-gate-edit"
          value={draft}
          rows={6}
          onChange={(event) => setDraft(event.target.value)}
          aria-label="edited prompt"
        />
      ) : (
        <pre className="output-block">{run.prompt}</pre>
      )}

      {busy ? (
        <div className="workflow-gate-busy">
          <Loader2 size={16} className="workflow-spin" /> Resuming…
        </div>
      ) : editing ? (
        <div className="panel-actions">
          <Button variant="primary" type="button" onClick={() => onEdit(draft)} title="Run with the edited prompt">
            <Check size={14} /> Save &amp; run
          </Button>
          <Button
            variant="ghost"
            type="button"
            onClick={() => {
              setEditing(false);
              setDraft(run.prompt);
            }}
          >
            Cancel
          </Button>
        </div>
      ) : (
        <div className="panel-actions">
          <Button variant="primary" type="button" onClick={onApprove} title="Approve — run the step as-is">
            <Check size={14} /> Approve
          </Button>
          <Button variant="ghost" type="button" onClick={() => setEditing(true)} title="Edit the prompt">
            <Pencil size={14} /> Edit
          </Button>
          <Button variant="ghost" type="button" onClick={onReject} title="Reject — mark the step failed">
            <X size={14} /> Reject
          </Button>
        </div>
      )}
    </div>
  );
}

// The resolved result of a resumed run — replaces its card in place after the action
// (final output, plus any failed step ids).
function ResolvedGateCard({ run, result }: { run: WorkflowPausedRun; result: WorkflowRunResult }) {
  return (
    <div className="workflow-gate-card">
      <div className="workflow-gate-head">
        <Check size={14} />
        <strong>{run.recipe_name}</strong>
        <span className="workflow-step-sub">{run.paused_step}</span>
      </div>
      {result.failed.length ? <p className="workflow-failed">Failed steps: {result.failed.join(", ")}</p> : null}
      <pre className="output-block">{result.output}</pre>
    </div>
  );
}

// "Pending" — the queue of runs parked at a `gate: human` step. Polls
// GET /api/plugins/workflows/runs (on mount + on the 5s interval) and, after each
// approve/edit/reject, invalidates it so a resolved run drops out. A resolved run's
// result stays pinned in place (its card is replaced by the output) until the next poll.
// The run the operator is already watching in the timeline is excluded — its gate
// renders inline there instead of twice.
function PendingGates({ excludeRunId }: { excludeRunId: string | null }) {
  const queryClient = useQueryClient();
  const { data } = useQuery(workflowRunsQuery());
  const runs = (data?.runs ?? []).filter((r) => r.run_id !== excludeRunId);
  const [results, setResults] = useState<Record<string, { run: WorkflowPausedRun; result: WorkflowRunResult }>>({});
  const [busyId, setBusyId] = useState<string | null>(null);

  const resume = useMutation({
    mutationFn: (v: { run: WorkflowPausedRun; action: "approve" | "edit" | "reject"; edits?: { prompt?: string } }) =>
      api.resumeWorkflow(v.run.run_id, { action: v.action, edits: v.edits }),
    onMutate: (v) => setBusyId(v.run.run_id),
    onSuccess: (result, v) => {
      if (result.paused) {
        // A downstream gate re-paused the SAME run — it must reappear as active, so a
        // stale pinned result can't hide it (QA panel blocker: multi-gate workflows).
        setResults((prev) => {
          const next = { ...prev };
          delete next[v.run.run_id];
          return next;
        });
      } else {
        setResults((prev) => ({ ...prev, [v.run.run_id]: { run: v.run, result } }));
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflowRuns });
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflowRunHistory });
    },
    onSettled: () => setBusyId(null),
  });

  // Active = still-paused runs we haven't resolved locally; resolved = their pinned output.
  const active = runs.filter((r) => !(r.run_id in results));
  const resolved = Object.values(results);
  if (!active.length && !resolved.length) return null;

  return (
    <section className="workflow-gates">
      <div className="workflow-gates-head">
        <h2>Pending</h2>
        {active.length ? <span className="workflow-gate-count">{active.length}</span> : null}
      </div>
      {active.map((run) => (
        <PendingGateCard
          key={run.run_id}
          run={run}
          busy={busyId === run.run_id}
          onApprove={() => resume.mutate({ run, action: "approve" })}
          onReject={() => resume.mutate({ run, action: "reject" })}
          onEdit={(prompt) => resume.mutate({ run, action: "edit", edits: { prompt } })}
        />
      ))}
      {resolved.map(({ run, result }) => (
        <ResolvedGateCard key={run.run_id} run={run} result={result} />
      ))}
      {resume.isError ? <p className="workflow-failed">{errMsg(resume.error)}</p> : null}
    </section>
  );
}

// The recipe's parallelism lanes, read-only: column N holds the steps whose longest
// dependency chain is N deep — steps sharing a column run in parallel.
function RecipeLanes({ steps }: { steps: WorkflowSummary["steps"] }) {
  const lanes = useMemo(() => computeLanes(steps), [steps]);
  const byId = useMemo(() => new Map(steps.map((s) => [s.id, s])), [steps]);
  return (
    <div className="workflow-lanes" aria-label="step order (columns run in parallel)">
      {lanes.map((lane, i) => (
        <div className="workflow-lane" key={i}>
          {lane.map((id) => (
            <span key={id} className="lane-chip" title={byId.get(id)?.subagent}>
              {id}
              {byId.get(id)?.gate ? <Pause size={10} aria-label="operator gate" /> : null}
            </span>
          ))}
        </div>
      ))}
    </div>
  );
}

// Run history — every recorded run (any status), newest first; a row opens its
// record in the timeline (live if still running, an inspection if terminal).
function RunHistory({ activeRunId, onOpen }: { activeRunId: string | null; onOpen: (runId: string) => void }) {
  const { data } = useQuery(workflowRunHistoryQuery());
  const runs = data?.runs ?? [];
  if (!runs.length) return null;

  return (
    <details className="run-history">
      <summary>
        <History size={13} /> History ({runs.length})
      </summary>
      <div className="run-history-list">
        {runs.map((r) => (
          <button
            key={r.run_id}
            type="button"
            className={`run-history-row${r.run_id === activeRunId ? " run-history-row-active" : ""}`}
            onClick={() => onOpen(r.run_id)}
          >
            <span className={`run-status run-status-${r.status}`}>{r.status}</span>
            <strong>{r.recipe_name}</strong>
            <span className="run-history-steps">
              {r.steps_done}/{r.steps_total} steps
              {r.failed.length ? ` · ${r.failed.length} failed` : ""}
            </span>
            <span className="run-history-when">{ago(r.updated_at ?? null)}</span>
          </button>
        ))}
      </div>
    </details>
  );
}

function WorkflowsBody() {
  const queryClient = useQueryClient();
  const { data: wfData } = useSuspenseQuery(workflowsQuery());
  const { data: subData } = useSuspenseQuery(subagentsQuery());
  const workflows = wfData.workflows;
  const subagentNames = (subData.subagents || []).map((s) => s.name).filter(Boolean);

  const [selected, setSelected] = useState<string>("");
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [building, setBuilding] = useState(false);
  const [editRecipe, setEditRecipe] = useState<WorkflowRecipe | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  // Effective selection: explicit pick, else the first recipe.
  const selectedName = selected || workflows[0]?.name || "";
  const current = useMemo(
    () => workflows.find((w) => w.name === selectedName) ?? null,
    [workflows, selectedName],
  );

  const invalidateWorkflows = () => queryClient.invalidateQueries({ queryKey: queryKeys.workflows });

  const start = useMutation({
    mutationFn: (v: { name: string; inputs: Record<string, unknown> }) =>
      api.startWorkflow(v.name, v.inputs),
    onSuccess: (r) => {
      setActiveRunId(r.run_id);
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflowRunHistory });
    },
  });
  const remove = useMutation({
    mutationFn: (name: string) => api.deleteWorkflow(name),
    onSuccess: () => setSelected(""),
    onSettled: invalidateWorkflows,
  });
  const edit = useMutation({
    mutationFn: (name: string) => api.workflowRecipe(name),
    onSuccess: (r) => {
      setEditRecipe(r.recipe);
      setBuilding(true);
    },
  });

  function selectRecipe(name: string) {
    setSelected(name);
    setActiveRunId(null);
    const recipe = workflows.find((w) => w.name === name);
    const seed: Record<string, string> = {};
    for (const inp of recipe?.inputs ?? []) {
      seed[inp.name] = inp.default != null ? String(inp.default) : "";
    }
    setInputs(seed);
  }

  const missingRequired = current
    ? current.inputs.filter((i) => i.required && !inputs[i.name]?.trim()).map((i) => i.name)
    : [];

  function doRun() {
    if (!current) return;
    const payload: Record<string, unknown> = {};
    for (const inp of current.inputs) {
      const v = inputs[inp.name];
      if (v == null || v === "") continue;
      // A declared structured type takes JSON: parse it so the engine gets a
      // real object/array, not the string "{...}" (which would render into
      // step prompts verbatim). Unparseable text falls through as the typed
      // string — the engine substitutes it as-is either way.
      if (inp.type === "object" || inp.type === "array") {
        try {
          payload[inp.name] = JSON.parse(v);
          continue;
        } catch {
          /* not JSON — send the raw string */
        }
      }
      payload[inp.name] = v;
    }
    start.mutate({ name: current.name, inputs: payload });
  }

  function closeBuilder() {
    setBuilding(false);
    setEditRecipe(null);
  }

  return (
    <>
      <PanelHeader
        title="Workflows"
        kicker={`step-by-step recipes the engine runs over subagents · ${workflows.length} recipe${workflows.length === 1 ? "" : "s"}`}
        actions={
          <>
            <Button
              icon
              variant="ghost"
              type="button"
              onClick={() => (building ? closeBuilder() : setBuilding(true))}
              title="New workflow"
            >
              <Plus size={16} />
            </Button>
            <Button icon variant="ghost" type="button" onClick={() => void invalidateWorkflows()} title="Refresh">
              <RefreshCw size={16} />
            </Button>
          </>
        }
      />

      <div className="stage-body">
        {building ? (
          <WorkflowBuilder
            subagents={subagentNames}
            initial={editRecipe ?? undefined}
            onCancel={closeBuilder}
            onSaved={(name) => {
              closeBuilder();
              void queryClient.invalidateQueries({ queryKey: queryKeys.workflows });
              setSelected(name);
            }}
          />
        ) : (
          <>
            <PendingGates excludeRunId={activeRunId} />

            {!workflows.length ? (
              <div className="subagent-row">
                <div>
                  <strong>No workflows registered</strong>
                  <span>Drop a recipe in the workflows directory, or have the agent save one.</span>
                </div>
              </div>
            ) : (
              <div className="workflow-picker">
                <label className="field">
                  <span>Recipe</span>
                  <DropdownSelect
                    value={selectedName}
                    onValueChange={(v) => selectRecipe(v)}
                    options={workflows.map((w) => ({ value: w.name, label: w.name }))}
                  />
                </label>
                {current ? (
                  <div className="workflow-picker-actions">
                    <Button
                      variant="ghost"
                      type="button"
                      onClick={() => edit.mutate(current.name)}
                      loading={edit.isPending}
                      title="Edit this workflow"
                    >
                      {edit.isPending ? null : <Pencil size={14} />} Edit
                    </Button>
                    <Button
                      variant="ghost"
                      type="button"
                      onClick={() => remove.mutate(current.name)}
                      title="Delete this workflow"
                    >
                      <Trash2 size={14} /> Delete
                    </Button>
                  </div>
                ) : null}
              </div>
            )}
            {edit.isError ? <p className="workflow-failed">{errMsg(edit.error)}</p> : null}

            {current ? (
              <>
                {current.description ? <p className="workflow-desc">{current.description}</p> : null}

                {!activeRunId ? <RecipeLanes steps={current.steps} /> : null}

                {current.inputs.length ? (
                  <div className="subagent-grid">
                    {current.inputs.map((inp) => (
                      <label className="field" key={inp.name} title={inp.description || undefined}>
                        <span>
                          {inp.name}
                          {inp.required ? " *" : ""}
                          {inp.type === "object" || inp.type === "array" ? " (JSON)" : ""}
                        </span>
                        <Input
                          value={inputs[inp.name] ?? ""}
                          onChange={(event) => setInputs((prev) => ({ ...prev, [inp.name]: event.target.value }))}
                          placeholder={
                            inp.description ||
                            (inp.default != null
                              ? `default: ${String(inp.default)}`
                              : inp.required
                                ? "required"
                                : "optional")
                          }
                        />
                      </label>
                    ))}
                  </div>
                ) : null}

                <div className="panel-actions">
                  <Button
                    variant="primary"
                    type="button"
                    onClick={doRun}
                    loading={start.isPending}
                    disabled={missingRequired.length > 0}
                    title={missingRequired.length ? `missing: ${missingRequired.join(", ")}` : "Run workflow"}
                  >
                    {start.isPending ? null : <Play size={16} />}
                    Run
                  </Button>
                </div>
                {start.isError ? <p className="workflow-failed">{errMsg(start.error)}</p> : null}
              </>
            ) : null}

            {activeRunId ? <RunTimeline runId={activeRunId} onClose={() => setActiveRunId(null)} /> : null}

            <RunHistory activeRunId={activeRunId} onOpen={(runId) => setActiveRunId(runId)} />
          </>
        )}
      </div>
    </>
  );
}

export function WorkflowsSurface() {
  return (
    <StagePanel label="workflows">
      <WorkflowsBody />
    </StagePanel>
  );
}
