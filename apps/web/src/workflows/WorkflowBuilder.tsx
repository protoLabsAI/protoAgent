import { Checkbox, DropdownSelect, Input, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { Plus, Save, Trash2, X } from "lucide-react";

import { useState } from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { useToast } from "@protolabsai/ui/overlays";

// Author a workflow recipe from the console (Sprint C): name + inputs + steps
// (id, subagent, prompt, depends_on) + output → POST /api/workflows, which
// validates against the live subagent registry + DAG and saves it (immediately
// runnable). Step ordering/parallelism is expressed via depends_on; the server
// is the source of truth for validity.

type Step = { id: string; subagent: string; prompt: string; dependsOn: string[] };
type Input = { name: string; required: boolean };

export function WorkflowBuilder({
  subagents,
  onSaved,
  onCancel,
}: {
  subagents: string[];
  onSaved: (name: string) => void;
  onCancel: () => void;
}) {
  const toast = useToast();
  const fallback = subagents[0] || "researcher";
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [inputs, setInputs] = useState<Input[]>([{ name: "topic", required: true }]);
  const [steps, setSteps] = useState<Step[]>([
    { id: "step1", subagent: fallback, prompt: "", dependsOn: [] },
  ]);
  const [output, setOutput] = useState("");
  const [saving, setSaving] = useState(false);

  const setStep = (i: number, patch: Partial<Step>) =>
    setSteps((s) => s.map((st, j) => (j === i ? { ...st, ...patch } : st)));
  const addStep = () =>
    setSteps((s) => [...s, { id: `step${s.length + 1}`, subagent: fallback, prompt: "", dependsOn: [] }]);
  const removeStep = (i: number) => setSteps((s) => s.filter((_, j) => j !== i));

  const toggleDep = (i: number, depId: string) =>
    setStep(i, {
      dependsOn: steps[i].dependsOn.includes(depId)
        ? steps[i].dependsOn.filter((d) => d !== depId)
        : [...steps[i].dependsOn, depId],
    });

  const valid =
    name.trim() !== "" &&
    steps.length > 0 &&
    steps.every((st) => st.id.trim() && st.subagent && st.prompt.trim());

  async function save() {
    setSaving(true);
    const last = steps[steps.length - 1].id.trim();
    const recipe: Record<string, unknown> = {
      name: name.trim(),
      version: 1,
      inputs: inputs
        .filter((i) => i.name.trim())
        .map((i) => ({ name: i.name.trim(), required: i.required })),
      steps: steps.map((st) => ({
        id: st.id.trim(),
        subagent: st.subagent,
        prompt: st.prompt,
        ...(st.dependsOn.length ? { depends_on: st.dependsOn } : {}),
      })),
      output: output.trim() || `{{steps.${last}.output}}`,
    };
    if (description.trim()) recipe.description = description.trim();
    try {
      const r = await api.saveWorkflow(recipe);
      const saved = r.name || name.trim();
      toast({ tone: "success", title: "Workflow saved", message: `${saved} is ready to run.` });
      onSaved(saved);
    } catch (e) {
      toast({ tone: "error", title: "Couldn't save workflow", message: errMsg(e) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="workflow-builder">
      <PanelHeader
        compact
        title="New workflow"
        actions={
          <Button icon variant="ghost" type="button" onClick={onCancel} title="Cancel">
            <X size={16} />
          </Button>
        }
      />

      <label className="field">
        <span>Name *</span>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="my-workflow" />
      </label>
      <label className="field">
        <span>Description</span>
        <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="optional" />
      </label>

      <div className="builder-section">
        <div className="builder-section-head">
          <span>Inputs</span>
          <Button variant="ghost" type="button" onClick={() => setInputs((x) => [...x, { name: "", required: false }])}>
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
              {steps.length > 1 && (
                <Button icon variant="ghost" type="button" onClick={() => removeStep(i)} title="Remove step">
                  <Trash2 size={14} />
                </Button>
              )}
            </div>
            <Textarea
              className="builder-prompt"
              value={step.prompt}
              rows={2}
              placeholder="Prompt for this step — use {{inputs.x}} and {{steps.other.output}}"
              onChange={(e) => setStep(i, { prompt: e.target.value })}
            />
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
          placeholder={`default: {{steps.${steps[steps.length - 1].id.trim() || "lastStep"}.output}}`}
        />
      </label>

      <div className="panel-actions">
        <Button variant="ghost" type="button" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <Button variant="primary" type="button" onClick={() => void save()} loading={saving} disabled={!valid}>
          {saving ? null : <Save size={16} />}
          Save workflow
        </Button>
      </div>
    </div>
  );
}
