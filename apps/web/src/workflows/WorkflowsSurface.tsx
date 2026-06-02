import { Loader2, Play, Plus, RefreshCw, Trash2, Workflow } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { api } from "../lib/api";
import type { WorkflowRunResult, WorkflowSummary } from "../lib/types";
import { WorkflowBuilder } from "./WorkflowBuilder";

// Operator surface for declarative workflow recipes (ADR 0002). Lists the
// registered recipes, shows the selected one's step DAG, collects its inputs,
// and runs it — surfacing each step's output plus any inline failures.

export function WorkflowsSurface({ onError }: { onError: (message: string) => void }) {
  const [workflows, setWorkflows] = useState<WorkflowSummary[] | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<WorkflowRunResult | null>(null);
  const [building, setBuilding] = useState(false);
  const [subagentNames, setSubagentNames] = useState<string[]>([]);

  async function load() {
    try {
      const r = await api.workflows();
      setWorkflows(r.workflows);
      if (r.workflows.length && !r.workflows.some((w) => w.name === selected)) {
        setSelected(r.workflows[0].name);
      }
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    }
  }
  useEffect(() => {
    void load();
    void api
      .subagents()
      .then((r) => setSubagentNames((r.subagents || []).map((s) => s.name).filter(Boolean)))
      .catch(() => setSubagentNames([]));
  }, []);

  async function removeWorkflow(name: string) {
    onError("");
    try {
      await api.deleteWorkflow(name);
      setSelected("");
      await load();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    }
  }

  const current = useMemo(
    () => workflows?.find((w) => w.name === selected) ?? null,
    [workflows, selected],
  );

  // Reset the inputs form (seed defaults) when the selected recipe changes.
  useEffect(() => {
    if (!current) return;
    const seed: Record<string, string> = {};
    for (const inp of current.inputs) {
      seed[inp.name] = inp.default != null ? String(inp.default) : "";
    }
    setInputs(seed);
    setResult(null);
  }, [selected]); // eslint-disable-line react-hooks/exhaustive-deps

  const missingRequired = useMemo(() => {
    if (!current) return [];
    return current.inputs.filter((i) => i.required && !inputs[i.name]?.trim()).map((i) => i.name);
  }, [current, inputs]);

  async function run() {
    if (!current) return;
    setRunning(true);
    setResult(null);
    onError("");
    try {
      const payload: Record<string, unknown> = {};
      for (const inp of current.inputs) {
        const v = inputs[inp.name];
        if (v != null && v !== "") payload[inp.name] = v;
      }
      const r = await api.runWorkflow(current.name, payload);
      setResult(r);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  return (
    <section className="panel stage-panel">
      <div className="panel-header">
        <div>
          <h1>Workflows</h1>
          <p className="panel-kicker">
            {workflows ? `${workflows.length} recipe${workflows.length === 1 ? "" : "s"}` : "loading…"}
          </p>
        </div>
        <div className="panel-actions">
          <button className="icon-button" type="button" onClick={() => setBuilding((b) => !b)} title="New workflow">
            <Plus size={16} />
          </button>
          <button className="icon-button" type="button" onClick={() => void load()} title="Refresh">
            <RefreshCw size={16} />
          </button>
        </div>
      </div>

      <div className="stage-body">
        {building ? (
          <WorkflowBuilder
            subagents={subagentNames}
            onCancel={() => setBuilding(false)}
            onSaved={(name) => {
              setBuilding(false);
              void load().then(() => setSelected(name));
            }}
          />
        ) : (
          <>
        {workflows && !workflows.length ? (
          <div className="subagent-row">
            <div>
              <strong>No workflows registered</strong>
              <span>Drop a recipe in the workflows directory, or have the agent save one.</span>
            </div>
          </div>
        ) : null}

        {workflows && workflows.length ? (
          <label className="field">
            <span>Recipe</span>
            <select value={selected} onChange={(event) => setSelected(event.target.value)}>
              {workflows.map((w) => (
                <option key={w.name} value={w.name}>
                  {w.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}

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
              <button
                className="primary-button"
                type="button"
                onClick={() => void run()}
                disabled={running || missingRequired.length > 0}
                title={missingRequired.length ? `missing: ${missingRequired.join(", ")}` : "Run workflow"}
              >
                {running ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
                Run
              </button>
              <button
                className="ghost-button"
                type="button"
                onClick={() => void removeWorkflow(current.name)}
                title="Delete this workflow"
              >
                <Trash2 size={14} /> Delete
              </button>
            </div>
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
    </section>
  );
}
