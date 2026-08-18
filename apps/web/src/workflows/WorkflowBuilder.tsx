import { Checkbox, DropdownSelect, Input, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { AlertTriangle, Plus, Save, Trash2, X } from "lucide-react";

import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { useToast } from "@protolabsai/ui/overlays";
import type { WorkflowRecipe } from "../lib/types";

// Author a workflow recipe from the console (Sprint C), or EDIT an existing one
// (`initial` = the full recipe from GET /{name}/recipe): name + inputs (with
// defaults) + steps (id, subagent, prompt, depends_on, `gate: human`) + output →
// POST /api/workflows, which validates against the live subagent registry + DAG
// and saves it (immediately runnable). The same checks run live while authoring
// (POST /validate, debounced) so mistakes surface before save. Step
// ordering/parallelism is expressed via depends_on; the server is the source of
// truth for validity. Editing preserves recipe/step fields the form doesn't
// manage (version, max_concurrency, per-step timeout).

type Step = { id: string; subagent: string; prompt: string; dependsOn: string[]; gate: boolean };
type InputRow = { name: string; required: boolean; default: string };

const MANAGED_KEYS = ["name", "description", "version", "inputs", "steps", "output"] as const;

function fromInitial(initial: WorkflowRecipe | undefined, fallback: string): { inputs: InputRow[]; steps: Step[] } {
  if (!initial) {
    return {
      inputs: [{ name: "topic", required: true, default: "" }],
      steps: [{ id: "step1", subagent: fallback, prompt: "", dependsOn: [], gate: false }],
    };
  }
  return {
    inputs: (initial.inputs ?? []).map((i) => ({
      name: String(i.name ?? ""),
      required: Boolean(i.required),
      default: i.default != null ? String(i.default) : "",
    })),
    steps: (initial.steps ?? []).map((s) => ({
      id: String(s.id ?? ""),
      subagent: String(s.subagent ?? fallback),
      prompt: String(s.prompt ?? ""),
      dependsOn: typeof s.depends_on === "string" ? [s.depends_on] : [...(s.depends_on ?? [])],
      gate: s.gate === "human",
    })),
  };
}

export function WorkflowBuilder({
  subagents,
  onSaved,
  onCancel,
  initial,
}: {
  subagents: string[];
  onSaved: (name: string) => void;
  onCancel: () => void;
  initial?: WorkflowRecipe;
}) {
  const toast = useToast();
  const editing = Boolean(initial);
  const fallback = subagents[0] || "researcher";
  const seeded = useMemo(() => fromInitial(initial, fallback), [initial, fallback]);
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [inputs, setInputs] = useState<InputRow[]>(seeded.inputs);
  const [steps, setSteps] = useState<Step[]>(seeded.steps);
  const [output, setOutput] = useState(initial?.output ?? "");
  const [saving, setSaving] = useState(false);
  const [liveErrors, setLiveErrors] = useState<string[]>([]);
  // The DS Textarea doesn't forward a ref — reach the DOM node through a wrapper.
  const promptBoxRefs = useRef<(HTMLDivElement | null)[]>([]);

  const setStep = (i: number, patch: Partial<Step>) =>
    setSteps((s) => s.map((st, j) => (j === i ? { ...st, ...patch } : st)));
  const addStep = () =>
    setSteps((s) => [...s, { id: `step${s.length + 1}`, subagent: fallback, prompt: "", dependsOn: [], gate: false }]);
  const removeStep = (i: number) => setSteps((s) => s.filter((_, j) => j !== i));

  const toggleDep = (i: number, depId: string) =>
    setStep(i, {
      dependsOn: steps[i].dependsOn.includes(depId)
        ? steps[i].dependsOn.filter((d) => d !== depId)
        : [...steps[i].dependsOn, depId],
    });

  // Click a chip → the template ref lands at the cursor of that step's prompt.
  const insertRef = (i: number, ref: string) => {
    const el = promptBoxRefs.current[i]?.querySelector("textarea") ?? null;
    const prompt = steps[i].prompt;
    const at = el ? (el.selectionStart ?? prompt.length) : prompt.length;
    setStep(i, { prompt: prompt.slice(0, at) + ref + prompt.slice(at) });
    if (el) {
      requestAnimationFrame(() => {
        el.focus();
        el.selectionStart = el.selectionEnd = at + ref.length;
      });
    }
  };

  const valid =
    name.trim() !== "" &&
    steps.length > 0 &&
    steps.every((st) => st.id.trim() && st.subagent && st.prompt.trim());

  function buildRecipe(): Record<string, unknown> {
    const last = steps[steps.length - 1]?.id.trim() || "step1";
    // Editing preserves fields the form doesn't manage (max_concurrency, per-step
    // timeout, …): start from the loaded document, overwrite what the form owns.
    const extras = Object.fromEntries(
      Object.entries(initial ?? {}).filter(([k]) => !(MANAGED_KEYS as readonly string[]).includes(k)),
    );
    const origById = new Map((initial?.steps ?? []).map((s) => [String(s.id), s]));
    const recipe: Record<string, unknown> = {
      ...extras,
      name: name.trim(),
      version: initial?.version ?? 1,
      inputs: inputs
        .filter((i) => i.name.trim())
        .map((i) => ({
          name: i.name.trim(),
          required: i.required,
          ...(i.default.trim() !== "" ? { default: i.default } : {}),
        })),
      steps: steps.map((st) => {
        const orig = origById.get(st.id.trim()) ?? {};
        const step: Record<string, unknown> = {
          ...orig,
          id: st.id.trim(),
          subagent: st.subagent,
          prompt: st.prompt,
        };
        if (st.dependsOn.length) step.depends_on = st.dependsOn;
        else delete step.depends_on;
        if (st.gate) step.gate = "human";
        else delete step.gate;
        return step;
      }),
      output: output.trim() || `{{steps.${last}.output}}`,
    };
    if (description.trim()) recipe.description = description.trim();
    return recipe;
  }

  // Live validation — the server's own checks (unknown subagent, bad dep, cycle,
  // template refs), debounced while typing. Advisory: save still enforces.
  useEffect(() => {
    if (!valid) {
      setLiveErrors([]);
      return;
    }
    const t = setTimeout(() => {
      api
        .validateWorkflow(buildRecipe())
        .then((r) => setLiveErrors(r.errors ?? []))
        .catch(() => setLiveErrors([]));
    }, 600);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- buildRecipe reads all form state below
  }, [name, description, inputs, steps, output, valid]);

  async function save() {
    setSaving(true);
    try {
      const r = await api.saveWorkflow(buildRecipe());
      const saved = r.name || name.trim();
      toast({ tone: "success", title: editing ? "Workflow updated" : "Workflow saved", message: `${saved} is ready to run.` });
      onSaved(saved);
    } catch (e) {
      toast({ tone: "error", title: "Couldn't save workflow", message: errMsg(e) });
    } finally {
      setSaving(false);
    }
  }

  const inputChips = inputs.filter((i) => i.name.trim()).map((i) => `{{inputs.${i.name.trim()}}}`);

  return (
    <div className="workflow-builder">
      <PanelHeader
        compact
        title={editing ? `Edit — ${initial?.name}` : "New workflow"}
        actions={
          <Button icon variant="ghost" type="button" onClick={onCancel} title="Cancel">
            <X size={16} />
          </Button>
        }
      />

      <label className="field">
        <span>Name *{editing ? " (fixed)" : ""}</span>
        <Input value={name} disabled={editing} onChange={(e) => setName(e.target.value)} placeholder="my-workflow" />
      </label>
      <label className="field">
        <span>Description</span>
        <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="optional" />
      </label>

      <div className="builder-section">
        <div className="builder-section-head">
          <span>Inputs</span>
          <Button
            variant="ghost"
            type="button"
            onClick={() => setInputs((x) => [...x, { name: "", required: false, default: "" }])}
          >
            <Plus size={13} /> add input
          </Button>
        </div>
        {inputs.map((inp, i) => (
          <div className="builder-row" key={i}>
            <Input
              value={inp.name}
              placeholder="input name"
              onChange={(e) => setInputs((x) => x.map((v, j) => (j === i ? { ...v, name: e.target.value } : v)))}
            />
            <Input
              value={inp.default}
              placeholder="default (optional)"
              onChange={(e) => setInputs((x) => x.map((v, j) => (j === i ? { ...v, default: e.target.value } : v)))}
            />
            <Checkbox
              className="checkbox-field"
              checked={inp.required}
              onCheckedChange={(c) => setInputs((x) => x.map((v, j) => (j === i ? { ...v, required: c } : v)))}
              label="required"
            />
            <Button icon variant="ghost" type="button" onClick={() => setInputs((x) => x.filter((_, j) => j !== i))} title="Remove">
              <Trash2 size={14} />
            </Button>
          </div>
        ))}
      </div>

      <div className="builder-section">
        <div className="builder-section-head">
          <span>Steps</span>
          <Button variant="ghost" type="button" onClick={addStep}>
            <Plus size={13} /> add step
          </Button>
        </div>
        {steps.map((step, i) => (
          <div className="builder-step" key={i}>
            <div className="builder-row">
              <Input
                value={step.id}
                placeholder="step id"
                onChange={(e) => setStep(i, { id: e.target.value })}
              />
              <DropdownSelect
                value={step.subagent}
                onValueChange={(v) => setStep(i, { subagent: v })}
                options={(subagents.length ? subagents : [fallback]).map((s) => ({ value: s, label: s }))}
              />
              <Checkbox
                className="checkbox-field"
                checked={step.gate}
                onCheckedChange={(c) => setStep(i, { gate: Boolean(c) })}
                label="operator gate"
                title="Pause for operator approval before this step runs (gate: human)"
              />
              {steps.length > 1 && (
                <Button icon variant="ghost" type="button" onClick={() => removeStep(i)} title="Remove step">
                  <Trash2 size={14} />
                </Button>
              )}
            </div>
            <div
              className="builder-prompt-box"
              ref={(el) => {
                promptBoxRefs.current[i] = el;
              }}
            >
              <Textarea
                className="builder-prompt"
                value={step.prompt}
                rows={2}
                placeholder="Prompt for this step — use {{inputs.x}} and {{steps.other.output}}"
                onChange={(e) => setStep(i, { prompt: e.target.value })}
              />
            </div>
            {(inputChips.length > 0 || steps.length > 1) && (
              <div className="builder-chips">
                {inputChips.map((chip) => (
                  <button key={chip} type="button" className="builder-chip" onClick={() => insertRef(i, chip)}>
                    {chip}
                  </button>
                ))}
                {steps
                  .filter((other, j) => j !== i && other.id.trim())
                  .map((other) => (
                    <button
                      key={`step-${other.id}`}
                      type="button"
                      className="builder-chip"
                      onClick={() => insertRef(i, `{{steps.${other.id.trim()}.output}}`)}
                    >
                      {`{{steps.${other.id.trim()}.output}}`}
                    </button>
                  ))}
              </div>
            )}
            {steps.filter((_, j) => j !== i).length > 0 && (
              <div className="builder-deps">
                <span>depends on:</span>
                {steps
                  .filter((_, j) => j !== i)
                  .map((other) => (
                    <Checkbox
                      key={other.id}
                      className="checkbox-field"
                      checked={step.dependsOn.includes(other.id)}
                      onCheckedChange={() => toggleDep(i, other.id)}
                      label={other.id || "(unnamed)"}
                    />
                  ))}
              </div>
            )}
          </div>
        ))}
      </div>

      <label className="field">
        <span>Output</span>
        <Input
          value={output}
          onChange={(e) => setOutput(e.target.value)}
          placeholder={`default: {{steps.${steps[steps.length - 1]?.id.trim() || "lastStep"}.output}}`}
        />
      </label>

      {liveErrors.length > 0 && (
        <div className="builder-errors" role="alert">
          <AlertTriangle size={13} />
          <ul>
            {liveErrors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="panel-actions">
        <Button variant="ghost" type="button" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <Button variant="primary" type="button" onClick={() => void save()} loading={saving} disabled={!valid}>
          {saving ? null : <Save size={16} />}
          {editing ? "Save changes" : "Save workflow"}
        </Button>
      </div>
    </div>
  );
}
