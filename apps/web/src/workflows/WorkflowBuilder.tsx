import { Checkbox, DropdownSelect, Input, Textarea } from "@protolabsai/ui/forms";
import { Badge, Button } from "@protolabsai/ui/primitives";
import { AlertTriangle, FileInput, FileOutput, Pause, Plus, Save, Settings2, Trash2, X } from "lucide-react";

import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { useToast } from "@protolabsai/ui/overlays";
import type { WorkflowRecipe } from "../lib/types";
import { computeLanes } from "./RunTimeline";

// Author a workflow recipe from the console, or EDIT an existing one
// (`initial` = the full recipe from GET /{name}/recipe). "Outline & Focus"
// layout: the LEFT column is the workflow's shape — step cards in order, the
// parallelism lanes, and entries for the workflow header, inputs, and output —
// and the RIGHT pane is a focused editor for whichever entry is selected, so
// the selected step's prompt (the actual work) gets the whole pane. The server
// stays the source of truth for validity: the same checks that gate save run
// live while authoring (POST /validate, debounced) and land as dots on the
// outline cards + inline messages in the focused editor. Editing preserves
// recipe/step/input fields the form doesn't manage (version, max_concurrency,
// per-step timeout, input annotations beyond the managed set).

type Step = { id: string; subagent: string; prompt: string; dependsOn: string[]; gate: boolean };
type InputRow = { name: string; required: boolean; default: string; type: string; description: string };
// What's focused in the editor pane: a section, or a step by array index.
type Focus = "workflow" | "inputs" | "output" | number;

const MANAGED_KEYS = ["name", "description", "version", "inputs", "steps", "output"] as const;
// The managed slice of an input row; anything else an author wrote rides along untouched.
const INPUT_TYPES = ["string", "object", "array", "number", "boolean"].map((t) => ({ value: t, label: t }));

function fromInitial(initial: WorkflowRecipe | undefined, fallback: string): { inputs: InputRow[]; steps: Step[] } {
  if (!initial) {
    return {
      inputs: [{ name: "topic", required: true, default: "", type: "string", description: "" }],
      steps: [{ id: "step1", subagent: fallback, prompt: "", dependsOn: [], gate: false }],
    };
  }
  // The server canonicalizes inputs to a list, but this loader must not crash
  // the whole Studio on a shape it didn't expect (a mapping-authored recipe
  // did exactly that, #2834) — normalize list | mapping | junk defensively.
  const rawInputs: unknown = initial.inputs;
  const inputRows: Record<string, unknown>[] = Array.isArray(rawInputs)
    ? rawInputs.filter((i): i is Record<string, unknown> => !!i && typeof i === "object")
    : rawInputs && typeof rawInputs === "object"
      ? Object.entries(rawInputs).map(([name, spec]) => ({
          name,
          ...(spec && typeof spec === "object" ? (spec as Record<string, unknown>) : {}),
        }))
      : [];
  return {
    inputs: inputRows.map((i) => ({
      name: String(i.name ?? ""),
      required: Boolean(i.required),
      default: i.default != null ? String(i.default) : "",
      type: typeof i.type === "string" && i.type ? i.type : "string",
      description: typeof i.description === "string" ? i.description : "",
    })),
    steps: (Array.isArray(initial.steps) ? initial.steps : []).map((s) => ({
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
  // Editing usually means a prompt; creating starts at the workflow header.
  const [focus, setFocus] = useState<Focus>(editing ? 0 : "workflow");
  // The DS Textarea doesn't forward a ref — reach the DOM node through a wrapper.
  const promptBoxRef = useRef<HTMLDivElement | null>(null);

  const setStep = (i: number, patch: Partial<Step>) =>
    setSteps((s) => s.map((st, j) => (j === i ? { ...st, ...patch } : st)));
  const setInput = (i: number, patch: Partial<InputRow>) =>
    setInputs((x) => x.map((v, j) => (j === i ? { ...v, ...patch } : v)));
  const addStep = () => {
    setSteps((s) => [...s, { id: `step${s.length + 1}`, subagent: fallback, prompt: "", dependsOn: [], gate: false }]);
    setFocus(steps.length);
  };
  const removeStep = (i: number) => {
    setSteps((s) => s.filter((_, j) => j !== i));
    setFocus(i > 0 ? i - 1 : "workflow");
  };

  const toggleDep = (i: number, depId: string) =>
    setStep(i, {
      dependsOn: steps[i].dependsOn.includes(depId)
        ? steps[i].dependsOn.filter((d) => d !== depId)
        : [...steps[i].dependsOn, depId],
    });

  // Click a chip → the template ref lands at the cursor of the FOCUSED prompt.
  const insertRef = (i: number, ref: string) => {
    const el = promptBoxRef.current?.querySelector("textarea") ?? null;
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
    // Same preservation contract as steps, keyed by name: the form manages
    // name/type/description/required/default; any other annotation survives.
    const origInputByName = new Map<string, Record<string, unknown>>(
      (Array.isArray(initial?.inputs) ? initial.inputs : [])
        .filter((i) => !!i && typeof i === "object")
        .map((i) => [String(i.name), i as unknown as Record<string, unknown>]),
    );
    const recipe: Record<string, unknown> = {
      ...extras,
      name: name.trim(),
      version: initial?.version ?? 1,
      inputs: inputs
        .filter((i) => i.name.trim())
        .map((i) => {
          const row: Record<string, unknown> = {
            ...(origInputByName.get(i.name.trim()) ?? {}),
            name: i.name.trim(),
            required: i.required,
          };
          if (i.default.trim() !== "") row.default = i.default;
          else delete row.default;
          if (i.type && i.type !== "string") row.type = i.type;
          else delete row.type;
          if (i.description.trim() !== "") row.description = i.description.trim();
          else delete row.description;
          return row;
        }),
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

  // Advisory mapping of server error strings onto outline cards: an error that
  // names a step id lands on that card; everything else lands on the workflow
  // card (and renders in whatever editor is open, so nothing hides).
  const errorsForStep = (id: string) => (id.trim() ? liveErrors.filter((e) => e.includes(id.trim())) : []);
  const unplacedErrors = liveErrors.filter((e) => !steps.some((s) => s.id.trim() && e.includes(s.id.trim())));

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
  const laneSteps = steps
    .filter((s) => s.id.trim())
    .map((s) => ({ id: s.id.trim(), subagent: s.subagent, depends_on: s.dependsOn, gate: s.gate ? "human" : undefined }));
  const lanes = useMemo(() => computeLanes(laneSteps), [steps]); // eslint-disable-line react-hooks/exhaustive-deps -- laneSteps derives from steps
  const focusedStep = typeof focus === "number" ? steps[focus] : undefined;

  const dot = (bad: boolean) => <span className={`builder-dot ${bad ? "builder-dot-err" : "builder-dot-ok"}`} />;

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

      <div className="builder-split">
        {/* ── outline: the workflow's shape, always visible ─────────────── */}
        <div className="builder-outline" role="tablist" aria-label="workflow outline">
          <button
            type="button"
            role="tab"
            aria-selected={focus === "workflow"}
            className={`builder-card ${focus === "workflow" ? "builder-card-sel" : ""}`}
            onClick={() => setFocus("workflow")}
          >
            <span className="builder-card-head">
              {dot(name.trim() === "" || unplacedErrors.length > 0)}
              <Settings2 size={12} />
              <strong>{name.trim() || "untitled workflow"}</strong>
            </span>
            <span className="builder-card-sub">{description.trim() || "name & description"}</span>
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={focus === "inputs"}
            className={`builder-card ${focus === "inputs" ? "builder-card-sel" : ""}`}
            onClick={() => setFocus("inputs")}
          >
            <span className="builder-card-head">
              <FileInput size={12} />
              <strong>Inputs</strong>
              <Badge>{inputs.filter((i) => i.name.trim()).length}</Badge>
            </span>
            <span className="builder-card-sub">
              {inputs.filter((i) => i.name.trim()).map((i) => i.name.trim()).join(", ") || "none declared"}
            </span>
          </button>

          {lanes.length > 1 && (
            <div className="builder-outline-lanes" aria-label="step order (columns run in parallel)">
              {lanes.map((lane, i) => (
                <div className="workflow-lane" key={i}>
                  {lane.map((id) => (
                    <span key={id} className="lane-chip">
                      {id}
                    </span>
                  ))}
                </div>
              ))}
            </div>
          )}

          {steps.map((step, i) => (
            <button
              type="button"
              role="tab"
              aria-selected={focus === i}
              className={`builder-card builder-card-step ${focus === i ? "builder-card-sel" : ""}`}
              key={i}
              onClick={() => setFocus(i)}
            >
              <span className="builder-card-head">
                {dot(!step.id.trim() || !step.prompt.trim() || errorsForStep(step.id).length > 0)}
                <strong>{step.id.trim() || "(unnamed)"}</strong>
                <Badge>{step.subagent}</Badge>
                {step.gate ? <Pause size={11} aria-label="operator gate" /> : null}
              </span>
              <span className="builder-card-sub">
                {step.dependsOn.length ? `after ${step.dependsOn.join(", ")} · ` : ""}
                {step.prompt.trim().split("\n")[0] || "empty prompt"}
              </span>
            </button>
          ))}

          <Button variant="ghost" type="button" className="builder-add-step" onClick={addStep}>
            <Plus size={13} /> add step
          </Button>

          <button
            type="button"
            role="tab"
            aria-selected={focus === "output"}
            className={`builder-card ${focus === "output" ? "builder-card-sel" : ""}`}
            onClick={() => setFocus("output")}
          >
            <span className="builder-card-head">
              <FileOutput size={12} />
              <strong>Output</strong>
            </span>
            <span className="builder-card-sub">
              {output.trim() || `{{steps.${steps[steps.length - 1]?.id.trim() || "lastStep"}.output}}`}
            </span>
          </button>
        </div>

        {/* ── focus editor: the selected thing gets the whole pane ──────── */}
        <div className="builder-focus">
          {focus === "workflow" && (
            <>
              <label className="field">
                <span>Name *{editing ? " (fixed)" : ""}</span>
                <Input value={name} disabled={editing} onChange={(e) => setName(e.target.value)} placeholder="my-workflow" />
              </label>
              <label className="field">
                <span>Description</span>
                <Textarea
                  rows={2}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="what this workflow does — shown in the run view"
                />
              </label>
            </>
          )}

          {focus === "inputs" && (
            <div className="builder-inputs">
              <div className="builder-section-head">
                <span>Declared inputs</span>
                <Button
                  variant="ghost"
                  type="button"
                  onClick={() => {
                    setInputs((x) => [...x, { name: "", required: false, default: "", type: "string", description: "" }]);
                  }}
                >
                  <Plus size={13} /> add input
                </Button>
              </div>
              {inputs.map((inp, i) => (
                <div className="builder-input-card" key={i}>
                  <div className="builder-row">
                    <Input value={inp.name} placeholder="input name" onChange={(e) => setInput(i, { name: e.target.value })} />
                    <DropdownSelect
                      value={inp.type}
                      onValueChange={(v) => setInput(i, { type: v })}
                      options={INPUT_TYPES}
                    />
                    <Checkbox
                      className="checkbox-field"
                      checked={inp.required}
                      onCheckedChange={(c) => setInput(i, { required: Boolean(c) })}
                      label="required"
                    />
                    <Button icon variant="ghost" type="button" onClick={() => setInputs((x) => x.filter((_, j) => j !== i))} title="Remove">
                      <Trash2 size={14} />
                    </Button>
                  </div>
                  <div className="builder-row builder-row-2">
                    <Input value={inp.default} placeholder="default (optional)" onChange={(e) => setInput(i, { default: e.target.value })} />
                    <Input
                      value={inp.description}
                      placeholder="description — the run form's field hint"
                      onChange={(e) => setInput(i, { description: e.target.value })}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}

          {focus === "output" && (
            <label className="field">
              <span>Output template</span>
              <Input
                value={output}
                onChange={(e) => setOutput(e.target.value)}
                placeholder={`default: {{steps.${steps[steps.length - 1]?.id.trim() || "lastStep"}.output}}`}
              />
            </label>
          )}

          {focusedStep && typeof focus === "number" && (
            <>
              <div className="builder-row builder-step-head">
                <Input value={focusedStep.id} placeholder="step id" onChange={(e) => setStep(focus, { id: e.target.value })} />
                <DropdownSelect
                  value={focusedStep.subagent}
                  onValueChange={(v) => setStep(focus, { subagent: v })}
                  options={(subagents.length ? subagents : [fallback]).map((s) => ({ value: s, label: s }))}
                />
                <Checkbox
                  className="checkbox-field"
                  checked={focusedStep.gate}
                  onCheckedChange={(c) => setStep(focus, { gate: Boolean(c) })}
                  label="operator gate"
                  title="Pause for operator approval before this step runs (gate: human)"
                />
                {steps.length > 1 && (
                  <Button icon variant="ghost" type="button" onClick={() => removeStep(focus)} title="Remove step">
                    <Trash2 size={14} />
                  </Button>
                )}
              </div>

              {steps.filter((_, j) => j !== focus).length > 0 && (
                <div className="builder-deps">
                  <span>after:</span>
                  {steps
                    .filter((_, j) => j !== focus)
                    .map((other) => (
                      <button
                        key={other.id}
                        type="button"
                        className={`builder-chip ${focusedStep.dependsOn.includes(other.id) ? "builder-chip-on" : ""}`}
                        onClick={() => toggleDep(focus, other.id)}
                        title={focusedStep.dependsOn.includes(other.id) ? "runs after this step — click to remove" : "click to run after this step"}
                      >
                        {other.id || "(unnamed)"}
                      </button>
                    ))}
                </div>
              )}

              <div className="builder-prompt-box" ref={promptBoxRef}>
                <Textarea
                  className="builder-prompt"
                  value={focusedStep.prompt}
                  rows={12}
                  placeholder="Prompt for this step — use {{inputs.x}} and {{steps.other.output}}"
                  onChange={(e) => setStep(focus, { prompt: e.target.value })}
                />
              </div>
              {(inputChips.length > 0 || steps.length > 1) && (
                <div className="builder-chips">
                  {inputChips.map((chip) => (
                    <button key={chip} type="button" className="builder-chip" onClick={() => insertRef(focus, chip)}>
                      {chip}
                    </button>
                  ))}
                  {steps
                    .filter((other, j) => j !== focus && other.id.trim())
                    .map((other) => (
                      <button
                        key={`step-${other.id}`}
                        type="button"
                        className="builder-chip"
                        onClick={() => insertRef(focus, `{{steps.${other.id.trim()}.output}}`)}
                      >
                        {`{{steps.${other.id.trim()}.output}}`}
                      </button>
                    ))}
                </div>
              )}
              {errorsForStep(focusedStep.id).length > 0 && (
                <div className="builder-errors" role="alert">
                  <AlertTriangle size={13} />
                  <ul>
                    {errorsForStep(focusedStep.id).map((e) => (
                      <li key={e}>{e}</li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}

          {unplacedErrors.length > 0 && typeof focus !== "number" && (
            <div className="builder-errors" role="alert">
              <AlertTriangle size={13} />
              <ul>
                {unplacedErrors.map((e) => (
                  <li key={e}>{e}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>

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
