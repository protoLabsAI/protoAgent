import { Checkbox, DropdownSelect, Input, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import {
  AlertTriangle,
  Copy,
  FileInput,
  FileOutput,
  Play,
  Plus,
  Save,
  Settings2,
  Trash2,
  X,
} from "lucide-react";

import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../lib/api";
import { errMsg } from "../lib/format";
import { PanelHeader } from "@protolabsai/ui/navigation";
import { useToast } from "@protolabsai/ui/overlays";
import type { WorkflowRecipe } from "../lib/types";
import { Menu, MenuItem, MenuSeparator } from "@protolabsai/ui/menu";

import { type BuilderStep, downstreamOf, uniqueStepId, upstreamOf } from "./builderOps";
import { DagCanvas } from "./DagCanvas";

// Author a workflow recipe from the console, or EDIT an existing one
// (`initial` = the full recipe from GET /{name}/recipe). The structure surface
// is a node-and-edge DAG canvas (n8n/ComfyUI shape — the operator's call after
// living with the outline): nodes are steps, edges are `depends_on`, dragging
// a connection creates a dependency (cycle-guarded), deleting an edge removes
// one, and node positions persist on the step's unmanaged `ui` key. Selecting
// a node opens the focused editor beside the canvas, where the prompt gets
// real room; the workflow header, inputs, and output open from the toolbar.
// The server stays the source of truth for validity: the same checks that
// gate save run live while authoring (POST /validate, debounced) and land as
// dots on the nodes + inline messages in the open editor. Editing preserves
// recipe/step/input fields the form doesn't manage.

type Step = BuilderStep;
type InputRow = { name: string; required: boolean; default: string; type: string; description: string };
// What the editor pane shows: a section, a step by index, or nothing (full canvas).
type Focus = "workflow" | "inputs" | "output" | number | null;

const MANAGED_KEYS = ["name", "description", "version", "inputs", "steps", "output"] as const;
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
      ui:
        s.ui && typeof s.ui === "object" && Number.isFinite(Number(s.ui.x)) && Number.isFinite(Number(s.ui.y))
          ? { x: Number(s.ui.x), y: Number(s.ui.y) }
          : undefined,
    })),
  };
}

export function WorkflowBuilder({
  subagents,
  onSaved,
  onCancel,
  initial,
  onTest,
}: {
  subagents: string[];
  onSaved: (name: string) => void;
  onCancel: () => void;
  initial?: WorkflowRecipe;
  /** Save, then land on the run form with this recipe selected and its
   * defaults seeded — the tightest author-iterate loop. */
  onTest?: (name: string) => void;
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
    setSteps((s) => [...s, { id: uniqueStepId(s, "step"), subagent: fallback, prompt: "", dependsOn: [], gate: false }]);
    setFocus(steps.length);
  };
  const removeStep = (i: number) => {
    const goneId = steps[i]?.id.trim();
    setSteps((s) =>
      s
        .filter((_, j) => j !== i)
        .map((st) => (goneId && st.dependsOn.includes(goneId) ? { ...st, dependsOn: st.dependsOn.filter((d) => d !== goneId) } : st)),
    );
    setFocus(null);
  };
  const duplicateStep = (i: number) => {
    const src = steps[i];
    const clone: Step = {
      ...src,
      id: uniqueStepId(steps, src.id),
      ui: src.ui ? { x: src.ui.x + 40, y: src.ui.y + 56 } : undefined,
    };
    setSteps((s) => [...s.slice(0, i + 1), clone, ...s.slice(i + 1)]);
    setFocus(i + 1);
  };

  const connect = (sourceId: string, targetId: string) =>
    setSteps((s) =>
      s.map((st) =>
        st.id.trim() === targetId && !st.dependsOn.includes(sourceId)
          ? { ...st, dependsOn: [...st.dependsOn, sourceId] }
          : st,
      ),
    );
  const disconnect = (sourceId: string, targetId: string) =>
    setSteps((s) =>
      s.map((st) => (st.id.trim() === targetId ? { ...st, dependsOn: st.dependsOn.filter((d) => d !== sourceId) } : st)),
    );

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
        if (st.ui) step.ui = st.ui;
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

  // Advisory mapping of server error strings onto nodes: an error naming a
  // step id lands on that node's dot; everything else surfaces in whatever
  // editor is open (and reddens the workflow toolbar chip).
  const errorsForStep = (id: string) => (id.trim() ? liveErrors.filter((e) => e.includes(id.trim())) : []);
  const unplacedErrors = liveErrors.filter((e) => !steps.some((s) => s.id.trim() && e.includes(s.id.trim())));

  async function save(then: (name: string) => void = onSaved) {
    setSaving(true);
    try {
      const r = await api.saveWorkflow(buildRecipe());
      const saved = r.name || name.trim();
      toast({ tone: "success", title: editing ? "Workflow updated" : "Workflow saved", message: `${saved} is ready to run.` });
      then(saved);
    } catch (e) {
      toast({ tone: "error", title: "Couldn't save workflow", message: errMsg(e) });
    } finally {
      setSaving(false);
    }
  }

  const focusedStep = typeof focus === "number" ? steps[focus] : undefined;
  const stepFlags = steps.map((st) => ({
    error: !st.id.trim() || !st.prompt.trim() || errorsForStep(st.id).length > 0,
  }));

  const chip = (key: Exclude<Focus, number | null>, icon: React.ReactNode, label: string, bad = false) => (
    <button
      type="button"
      className={`builder-toolchip ${focus === key ? "builder-toolchip-on" : ""} ${bad ? "builder-toolchip-bad" : ""}`}
      onClick={() => setFocus(focus === key ? null : key)}
    >
      {icon} {label}
    </button>
  );

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

      <div className="builder-toolbar">
        {chip("workflow", <Settings2 size={12} />, name.trim() || "workflow", name.trim() === "" || unplacedErrors.length > 0)}
        {chip("inputs", <FileInput size={12} />, `inputs · ${inputs.filter((i) => i.name.trim()).length}`)}
        {chip("output", <FileOutput size={12} />, "output")}
        <span className="builder-toolbar-spacer" />
        <Button variant="ghost" type="button" onClick={addStep}>
          <Plus size={13} /> add step
        </Button>
      </div>

      <div className="builder-stage">
        <DagCanvas
          steps={steps}
          focusIndex={typeof focus === "number" ? focus : null}
          stepFlags={stepFlags}
          onSelect={(i) => setFocus(i)}
          onConnect={connect}
          onDisconnect={disconnect}
          onMove={(i, pos) => setStep(i, { ui: pos })}
          onCycleRefused={() =>
            toast({ tone: "error", title: "That edge would loop", message: "The target already runs before the source." })
          }
        />

        {focus != null && (
          <div className="builder-editor">
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
                      <DropdownSelect value={inp.type} onValueChange={(v) => setInput(i, { type: v })} options={INPUT_TYPES} />
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
                  <Button icon variant="ghost" type="button" onClick={() => duplicateStep(focus)} title="Duplicate step">
                    <Copy size={14} />
                  </Button>
                  {steps.length > 1 && (
                    <Button icon variant="ghost" type="button" onClick={() => removeStep(focus)} title="Remove step">
                      <Trash2 size={14} />
                    </Button>
                  )}
                </div>

                {(() => {
                  // The pills mirror the canvas edges for anyone who'd rather
                  // click than drag a connection; same cycle guard.
                  const cyclic = downstreamOf(steps, focusedStep.id);
                  const candidates = steps.filter((other, j) => j !== focus && !cyclic.has(other.id));
                  return candidates.length > 0 ? (
                    <div className="builder-deps">
                      <span>after:</span>
                      {candidates.map((other) => (
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
                  ) : null;
                })()}

                {(() => {
                  // One compact grouped picker instead of a chip wall (13 pills
                  // on code-review was unreadable). It offers only what the
                  // prompt can actually READ at run time: declared inputs, and
                  // the outputs of ANCESTOR steps — a non-upstream step's
                  // output would render empty.
                  const inputNames = inputs.map((i) => i.name.trim()).filter(Boolean);
                  const ancestors = upstreamOf(steps, focusedStep.id);
                  const upstreamSteps = steps.filter((s) => ancestors.has(s.id.trim()));
                  if (!inputNames.length && !upstreamSteps.length) return null;
                  return (
                    <div className="builder-insert-row">
                      <Menu
                        trigger={
                          <button type="button" className="builder-toolchip" title="Insert a template variable at the cursor">
                            {"{{…}}"} insert variable
                          </button>
                        }
                        align="start"
                      >
                        {inputNames.length > 0 && (
                          <div className="builder-menu-label" role="presentation">
                            Inputs
                          </div>
                        )}
                        {inputNames.map((n) => (
                          <MenuItem key={n} onSelect={() => insertRef(focus, `{{inputs.${n}}}`)}>
                            {`{{inputs.${n}}}`}
                          </MenuItem>
                        ))}
                        {inputNames.length > 0 && upstreamSteps.length > 0 ? <MenuSeparator /> : null}
                        {upstreamSteps.length > 0 && (
                          <div className="builder-menu-label" role="presentation">
                            Upstream step outputs
                          </div>
                        )}
                        {upstreamSteps.map((s) => (
                          <MenuItem key={s.id} onSelect={() => insertRef(focus, `{{steps.${s.id.trim()}.output}}`)}>
                            {`{{steps.${s.id.trim()}.output}}`}
                          </MenuItem>
                        ))}
                      </Menu>
                    </div>
                  );
                })()}
                <div className="builder-prompt-box" ref={promptBoxRef}>
                  <Textarea
                    className="builder-prompt"
                    value={focusedStep.prompt}
                    rows={10}
                    placeholder="Prompt for this step — use {{inputs.x}} and {{steps.other.output}}"
                    onChange={(e) => setStep(focus, { prompt: e.target.value })}
                  />
                </div>
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
        )}
      </div>

      <div className="panel-actions">
        <Button variant="ghost" type="button" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        {onTest && (
          <Button
            type="button"
            onClick={() => void save(onTest)}
            disabled={!valid || saving}
            title="Save, then land on the run form with this recipe selected"
          >
            <Play size={15} /> Save &amp; test
          </Button>
        )}
        <Button variant="primary" type="button" onClick={() => void save()} loading={saving} disabled={!valid}>
          {saving ? null : <Save size={16} />}
          {editing ? "Save changes" : "Save workflow"}
        </Button>
      </div>
    </div>
  );
}
