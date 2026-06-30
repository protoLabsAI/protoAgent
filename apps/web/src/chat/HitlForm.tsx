import "./hitl.css";
import { Checkbox, DropdownSelect, Input, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { useState } from "react";


import type { HitlPayload } from "../lib/types";
import {
  type FieldSchema,
  anyStepMissing,
  fieldsOf,
  isCardChoice,
  isMultiChoice,
  missingInStep,
  optionsOf,
} from "./hitl-form";

// A lightweight JSON-schema form for HITL requests (request_user_input) and a plain prompt
// for ask_human. Renders the common field types (string/number/boolean/enum/textarea) plus
// AskUserQuestion-style option cards (a field with `oneOf` [{const,title,description}], single
// or multi-select). Multi-step payloads render as a sequential Back/Next wizard with a step
// indicator; a single step shows just the fields + Submit. All steps' answers are collected
// into one object and submitted together.

function Field({
  name,
  schema,
  required,
  value,
  onChange,
}: {
  name: string;
  schema: FieldSchema;
  required: boolean;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const label = (schema.title || name) + (required ? " *" : "");

  // Option cards (AskUserQuestion-style): single-select (radio) or multi-select (checkbox).
  if (isCardChoice(schema)) {
    const multi = isMultiChoice(schema);
    const selected = new Set(
      multi
        ? (Array.isArray(value) ? value : []).map(String)
        : value != null && value !== ""
          ? [String(value)]
          : [],
    );
    const toggle = (v: string) => {
      if (!multi) {
        onChange(v);
        return;
      }
      const next = new Set(selected);
      if (next.has(v)) next.delete(v);
      else next.add(v);
      onChange([...next]);
    };
    return (
      <div className="hitl-field hitl-field-choice">
        <span>{label}</span>
        <div className="hitl-cards" role={multi ? "group" : "radiogroup"} aria-label={schema.title || name}>
          {optionsOf(schema).map((opt) => {
            const on = selected.has(opt.value);
            return (
              <button
                key={opt.value}
                type="button"
                className="hitl-card-option"
                role={multi ? "checkbox" : "radio"}
                aria-checked={on}
                data-selected={on || undefined}
                onClick={() => toggle(opt.value)}
              >
                <span className="hitl-card-mark" aria-hidden>
                  {on ? (multi ? "☑" : "◉") : multi ? "☐" : "○"}
                </span>
                <span className="hitl-card-body">
                  <span className="hitl-card-label">{opt.label}</span>
                  {opt.description && <span className="hitl-card-desc">{opt.description}</span>}
                </span>
              </button>
            );
          })}
        </div>
        {schema.description && <small>{schema.description}</small>}
      </div>
    );
  }

  if (schema.type === "boolean") {
    return (
      <Checkbox
        className="hitl-field hitl-field-bool"
        checked={Boolean(value)}
        onCheckedChange={onChange}
        label={label}
      />
    );
  }

  let control;
  if (Array.isArray(schema.enum)) {
    control = (
      <DropdownSelect
        value={String(value ?? "")}
        onValueChange={(v) => onChange(v)}
        placeholder="Select…"
        options={schema.enum.map((opt) => ({ value: String(opt), label: String(opt) }))}
      />
    );
  } else if (schema.type === "number" || schema.type === "integer") {
    control = (
      <Input
        type="number"
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
      />
    );
  } else if (schema.format === "textarea") {
    control = <Textarea value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} rows={3} />;
  } else {
    control = <Input type="text" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />;
  }

  return (
    <label className="hitl-field">
      <span>{label}</span>
      {control}
      {schema.description && <small>{schema.description}</small>}
    </label>
  );
}

export function HitlForm({
  payload,
  busy,
  onSubmit,
  onCancel,
  onApproveAlways,
}: {
  payload: HitlPayload;
  busy?: boolean;
  onSubmit: (response: Record<string, unknown> | string) => void;
  onCancel: () => void;
  // Approval gates only: "Approve & don't ask again" — approve this action AND turn on
  // bypass-permissions for the tab (skip future approvals). Omitted ⇒ the button is hidden.
  onApproveAlways?: () => void;
}) {
  const steps = payload.steps || [];
  const isForm = payload.kind === "form" && steps.length > 0;
  const isApproval = payload.kind === "approval";
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [text, setText] = useState("");
  const [current, setCurrent] = useState(0);

  // Approval gate (e.g. run_command) — Approve / Deny on the action.
  if (isApproval) {
    return (
      <div className="hitl-card hitl-approval" role="dialog" aria-label="Approval requested">
        <div className="hitl-title">{payload.title || "Approve this action?"}</div>
        {payload.description && <div className="hitl-prompt">{payload.description}</div>}
        {payload.detail && <pre className="hitl-detail">{payload.detail}</pre>}
        <div className="hitl-actions">
          <Button type="button" variant="ghost" onClick={() => onSubmit("denied")} disabled={busy}>
            Deny
          </Button>
          {onApproveAlways && (
            <Button
              type="button"
              variant="ghost"
              className="hitl-approve-always"
              title="Approve this — and skip approval for the rest of this tab's commands (bypass mode). Turn off with /bypass off."
              onClick={onApproveAlways}
              disabled={busy}
            >
              Approve &amp; don&apos;t ask again
            </Button>
          )}
          <Button type="button" variant="primary" onClick={() => onSubmit("approved")} disabled={busy}>
            Approve
          </Button>
        </div>
      </div>
    );
  }

  // ask_human / free-text question.
  if (!isForm) {
    const prompt = payload.question || payload.description || payload.title || "Input requested.";
    return (
      <div className="hitl-card" role="dialog" aria-label="Input requested">
        <div className="hitl-title">{payload.title || "Input requested"}</div>
        <div className="hitl-prompt">{prompt}</div>
        <Textarea
          className="hitl-freetext"
          value={text}
          autoFocus
          placeholder="Your answer…"
          onChange={(e) => setText(e.target.value)}
        />
        <div className="hitl-actions">
          <Button type="button" variant="ghost" onClick={onCancel} disabled={busy}>
            Dismiss
          </Button>
          <Button
            type="button"
            variant="primary"
            onClick={() => onSubmit(text.trim())}
            disabled={busy || !text.trim()}
          >
            Send
          </Button>
        </div>
      </div>
    );
  }

  // request_user_input — a stepped wizard. One step per screen with Back/Next; the last step
  // submits all collected answers together. A single step drops the navigation chrome.
  const set = (k: string, v: unknown) => setValues((prev) => ({ ...prev, [k]: v }));
  const stepIdx = Math.min(current, steps.length - 1);
  const step = steps[stepIdx];
  const onLast = stepIdx >= steps.length - 1;
  const stepBlocked = missingInStep(step, values).length > 0; // gates Next on this step
  const submitBlocked = anyStepMissing(steps, values); // gates final Submit across all steps
  const multiStep = steps.length > 1;

  return (
    <div className="hitl-card" role="dialog" aria-label={payload.title || "Form requested"}>
      <div className="hitl-wizard-head">
        <div className="hitl-title">{payload.title || "Input requested"}</div>
        {multiStep && (
          <div className="hitl-stepper" aria-label={`Step ${stepIdx + 1} of ${steps.length}`}>
            <span className="hitl-step-count">
              Step {stepIdx + 1} / {steps.length}
            </span>
            <span className="hitl-dots" aria-hidden>
              {steps.map((_, i) => (
                <span
                  key={i}
                  className="hitl-dot"
                  data-state={i === stepIdx ? "active" : i < stepIdx ? "done" : "todo"}
                />
              ))}
            </span>
          </div>
        )}
      </div>
      {payload.description && <div className="hitl-prompt">{payload.description}</div>}

      <div className="hitl-step" key={stepIdx}>
        {step.title && <div className="hitl-step-title">{step.title}</div>}
        {step.description && <div className="hitl-prompt">{step.description}</div>}
        {fieldsOf(step).map(([key, schema, req]) => (
          <Field
            key={key}
            name={key}
            schema={schema}
            required={req}
            value={values[key]}
            onChange={(v) => set(key, v)}
          />
        ))}
      </div>

      <div className="hitl-actions">
        <Button type="button" variant="ghost" onClick={onCancel} disabled={busy}>
          Dismiss
        </Button>
        {multiStep && stepIdx > 0 && (
          <Button
            type="button"
            variant="ghost"
            onClick={() => setCurrent((c) => Math.max(0, c - 1))}
            disabled={busy}
          >
            Back
          </Button>
        )}
        {!onLast ? (
          <Button
            type="button"
            variant="primary"
            onClick={() => setCurrent((c) => Math.min(steps.length - 1, c + 1))}
            disabled={busy || stepBlocked}
          >
            Next
          </Button>
        ) : (
          <Button
            type="button"
            variant="primary"
            onClick={() => onSubmit(values)}
            disabled={busy || submitBlocked}
          >
            Submit
          </Button>
        )}
      </div>
    </div>
  );
}
