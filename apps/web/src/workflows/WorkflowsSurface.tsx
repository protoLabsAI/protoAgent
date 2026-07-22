import "./workflows.css";

import { DropdownSelect, Input } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import {
  useMutation,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Check, Loader2, Pencil, Play, Plus, RefreshCw, Trash2, Workflow, X } from "lucide-react";
import { useMemo, useState } from "react";

import { StagePanel } from "../app/ErrorBoundary";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { api } from "../lib/api";
import { queryKeys, subagentsQuery, workflowRunsQuery, workflowsQuery } from "../lib/queries";
import type { WorkflowPausedRun, WorkflowRunResult } from "../lib/types";
import { WorkflowBuilder } from "./WorkflowBuilder";

// Operator surface for declarative workflow recipes (ADR 0002), on the TanStack
// Query data layer (ADR 0013): the recipe list + subagent registry are
// `useSuspenseQuery` reads; run/delete are `useMutation`s; loading is a
// <Suspense> fallback and errors a contained <ErrorBoundary>.

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
        <span className="workflow-step-sub">{run.paused_at}</span>
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
        <span className="workflow-step-sub">{run.paused_at}</span>
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
function PendingGates() {
  const queryClient = useQueryClient();
  const { data } = useQuery(workflowRunsQuery());
  const runs = data?.runs ?? [];
  const [results, setResults] = useState<Record<string, { run: WorkflowPausedRun; result: WorkflowRunResult }>>({});
  const [busyId, setBusyId] = useState<string | null>(null);

  const resume = useMutation({
    mutationFn: (v: { run: WorkflowPausedRun; action: "approve" | "edit" | "reject"; edits?: { prompt?: string } }) =>
      api.resumeWorkflow(v.run.run_id, { action: v.action, edits: v.edits }),
    onMutate: (v) => setBusyId(v.run.run_id),
    onSuccess: (result, v) => {
      setResults((prev) => ({ ...prev, [v.run.run_id]: { run: v.run, result } }));
      void queryClient.invalidateQueries({ queryKey: queryKeys.workflowRuns });
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
      {resume.isError ? <p className="workflow-failed">{(resume.error as Error).message}</p> : null}
    </section>
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
  const [result, setResult] = useState<WorkflowRunResult | null>(null);
  const [building, setBuilding] = useState(false);

  // Effective selection: explicit pick, else the first recipe.
  const selectedName = selected || workflows[0]?.name || "";
  const current = useMemo(
    () => workflows.find((w) => w.name === selectedName) ?? null,
    [workflows, selectedName],
  );

  const invalidateWorkflows = () => queryClient.invalidateQueries({ queryKey: queryKeys.workflows });

  const run = useMutation({
    mutationFn: (v: { name: string; inputs: Record<string, unknown> }) =>
      api.runWorkflow(v.name, v.inputs),
    onSuccess: (r) => setResult(r),
  });
  const remove = useMutation({
    mutationFn: (name: string) => api.deleteWorkflow(name),
    onSuccess: () => setSelected(""),
    onSettled: invalidateWorkflows,
  });

  function selectRecipe(name: string) {
    setSelected(name);
    setResult(null);
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
    setResult(null);
    const payload: Record<string, unknown> = {};
    for (const inp of current.inputs) {
      const v = inputs[inp.name];
      if (v != null && v !== "") payload[inp.name] = v;
    }
    run.mutate({ name: current.name, inputs: payload });
  }

  return (
    <>
      <PanelHeader
        title="Workflows"
        kicker={`step-by-step recipes the engine runs over subagents · ${workflows.length} recipe${workflows.length === 1 ? "" : "s"}`}
        actions={
          <>
            <Button icon variant="ghost" type="button" onClick={() => setBuilding((b) => !b)} title="New workflow">
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
            onCancel={() => setBuilding(false)}
            onSaved={(name) => {
              setBuilding(false);
              void queryClient.invalidateQueries({ queryKey: queryKeys.workflows });
              setSelected(name);
            }}
          />
        ) : (
          <>
            <PendingGates />

            {!workflows.length ? (
              <div className="subagent-row">
                <div>
                  <strong>No workflows registered</strong>
                  <span>Drop a recipe in the workflows directory, or have the agent save one.</span>
                </div>
              </div>
            ) : (
              <label className="field">
                <span>Recipe</span>
                <DropdownSelect
                  value={selectedName}
                  onValueChange={(v) => selectRecipe(v)}
                  options={workflows.map((w) => ({ value: w.name, label: w.name }))}
                />
              </label>
            )}

            {current ? (
              <>
                {current.description ? <p className="workflow-desc">{current.description}</p> : null}

                <div className="workflow-steps">
                  {current.steps.map((step) => (
                    <div className="workflow-step" key={step.id}>
                      <Workflow size={14} />
                      <strong>{step.id}</strong>
                      <span className="workflow-step-sub">{step.subagent}</span>
                      {step.depends_on.length ? (
                        <span className="workflow-step-dep">after {step.depends_on.join(", ")}</span>
                      ) : null}
                    </div>
                  ))}
                </div>

                {current.inputs.length ? (
                  <div className="subagent-grid">
                    {current.inputs.map((inp) => (
                      <label className="field" key={inp.name}>
                        <span>
                          {inp.name}
                          {inp.required ? " *" : ""}
                        </span>
                        <Input
                          value={inputs[inp.name] ?? ""}
                          onChange={(event) => setInputs((prev) => ({ ...prev, [inp.name]: event.target.value }))}
                          placeholder={inp.default != null ? `default: ${String(inp.default)}` : inp.required ? "required" : "optional"}
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
                    loading={run.isPending}
                    disabled={missingRequired.length > 0}
                    title={missingRequired.length ? `missing: ${missingRequired.join(", ")}` : "Run workflow"}
                  >
                    {run.isPending ? null : <Play size={16} />}
                    Run
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
                {run.isError ? <p className="workflow-failed">{(run.error as Error).message}</p> : null}
              </>
            ) : null}

            {result ? (
              <div className="workflow-result">
                {result.failed.length ? (
                  <p className="workflow-failed">Failed steps: {result.failed.join(", ")}</p>
                ) : null}
                <h2>Output</h2>
                <pre className="output-block">{result.output}</pre>
                {Object.keys(result.steps).length ? (
                  <details>
                    <summary>Per-step output ({Object.keys(result.steps).length})</summary>
                    {Object.entries(result.steps).map(([id, out]) => (
                      <div className="workflow-step-out" key={id}>
                        <strong>{id}</strong>
                        <pre className="output-block">{out}</pre>
                      </div>
                    ))}
                  </details>
                ) : null}
              </div>
            ) : null}
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
