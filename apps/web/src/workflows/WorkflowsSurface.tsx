import { Button } from "@protolabsai/ui/primitives";
import {

  QueryErrorResetBoundary,
  useMutation,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Loader2, Play, Plus, RefreshCw, Trash2, Workflow } from "lucide-react";
import { Suspense, useMemo, useState } from "react";

import { ErrorBoundary, PanelError, PanelSkeleton } from "../app/ErrorBoundary";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { api } from "../lib/api";
import { queryKeys, subagentsQuery, workflowsQuery } from "../lib/queries";
import type { WorkflowRunResult } from "../lib/types";
import { WorkflowBuilder } from "./WorkflowBuilder";

// Operator surface for declarative workflow recipes (ADR 0002), on the TanStack
// Query data layer (ADR 0013): the recipe list + subagent registry are
// `useSuspenseQuery` reads; run/delete are `useMutation`s; loading is a
// <Suspense> fallback and errors a contained <ErrorBoundary>.

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
                <select value={selectedName} onChange={(event) => selectRecipe(event.target.value)}>
                  {workflows.map((w) => (
                    <option key={w.name} value={w.name}>
                      {w.name}
                    </option>
                  ))}
                </select>
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
                        <input
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
                    disabled={run.isPending || missingRequired.length > 0}
                    title={missingRequired.length ? `missing: ${missingRequired.join(", ")}` : "Run workflow"}
                  >
                    {run.isPending ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
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
    <section className="panel stage-panel">
      <QueryErrorResetBoundary>
        {({ reset }) => (
          <ErrorBoundary onReset={reset} fallback={(a) => <PanelError {...a} label="workflows" />}>
            <Suspense fallback={<PanelSkeleton label="Loading workflows…" />}>
              <WorkflowsBody />
            </Suspense>
          </ErrorBoundary>
        )}
      </QueryErrorResetBoundary>
    </section>
  );
}
