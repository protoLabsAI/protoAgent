import "./hitl.css";
import { Checkbox, DropdownSelect, Input, Textarea } from "@protolabsai/ui/forms";
import { Button } from "@protolabsai/ui/primitives";
import { useEffect, useRef, useState } from "react";


import type { HitlPayload } from "../lib/types";
import {
  type FieldSchema,
  anyStepMissing,
  isCardChoice,
  isMultiChoice,
  missingInStep,
  optionsOf,
  seedDefaults,
  visibleFieldsOf,
} from "./hitl-form";

// A lightweight JSON-schema form for HITL requests (request_user_input) and a plain prompt
// for ask_human. Renders the common field types (string/number/boolean/enum/textarea) plus
// AskUserQuestion-style option cards (a field with `oneOf` [{const,title,description}], single
// or multi-select). Multi-step payloads render as a sequential Back/Next wizard with a step
// indicator; a single step shows just the fields + Submit. All steps' answers are collected
// into one object and submitted together. Answers start prefilled from the schemas'
// `default`s, and the card is fully keyboard-operable (#1978): it takes focus on appear,
// ←/→ move card selection, Enter confirms, Esc dismisses, and focus returns to wherever
// it was (the composer, in the chat host) on close.

// Option cards (AskUserQuestion-style): single-select (radio) or multi-select (checkbox).
// Keyboard (#1978) — a roving tabindex: Tab enters the group on one card (the selected/
// default one), ←/→/↑/↓ move between cards (and, for radios, select — selection follows
// focus, the WAI-ARIA radio-group pattern), Space picks, Enter confirms the whole form.
function CardChoiceField({
  name,
  schema,
  label,
  value,
  onChange,
  onConfirm,
}: {
  name: string;
  schema: FieldSchema;
  label: string;
  value: unknown;
  onChange: (v: unknown) => void;
  onConfirm?: () => void;
}) {
  const multi = isMultiChoice(schema);
  const options = optionsOf(schema);
  const selected = new Set(
    multi
      ? (Array.isArray(value) ? value : []).map(String)
      : value != null && value !== ""
        ? [String(value)]
        : [],
  );
  // The roving anchor — which card is tabbable. Starts on the selected card (the seeded
  // default when one exists) so Tab lands there and Enter can confirm it immediately.
  const [rove, setRove] = useState(() => Math.max(0, options.findIndex((o) => selected.has(o.value))));
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
  const onCardKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, idx: number) => {
    if (event.key === "Enter") {
      // Enter confirms the form (Next/Submit), Space picks — without the preventDefault
      // the button would just "click" (re-pick the focused card) instead.
      event.preventDefault();
      onConfirm?.();
      return;
    }
    const dir =
      event.key === "ArrowRight" || event.key === "ArrowDown"
        ? 1
        : event.key === "ArrowLeft" || event.key === "ArrowUp"
          ? -1
          : 0;
    if (!dir) return;
    event.preventDefault();
    const next = (idx + dir + options.length) % options.length;
    setRove(next);
    if (!multi) onChange(options[next].value); // radios: selection follows focus
    const cards = event.currentTarget
      .closest(".hitl-cards")
      ?.querySelectorAll<HTMLElement>(".hitl-card-option");
    cards?.[next]?.focus();
  };
  return (
    <div className="hitl-field hitl-field-choice">
      <span>{label}</span>
      <div className="hitl-cards" role={multi ? "group" : "radiogroup"} aria-label={schema.title || name}>
        {options.map((opt, idx) => {
          const on = selected.has(opt.value);
          return (
            <button
              key={opt.value}
              type="button"
              className="hitl-card-option"
              role={multi ? "checkbox" : "radio"}
              aria-checked={on}
              data-selected={on || undefined}
              tabIndex={idx === rove ? 0 : -1}
              onKeyDown={(e) => onCardKeyDown(e, idx)}
              onClick={() => {
                setRove(idx);
                toggle(opt.value);
              }}
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

function Field({
  name,
  schema,
  required,
  value,
  onChange,
  onConfirm,
}: {
  name: string;
  schema: FieldSchema;
  required: boolean;
  value: unknown;
  onChange: (v: unknown) => void;
  // Enter's action from within a field — the same as the step's primary button
  // (Next / Submit), with the same gating.
  onConfirm?: () => void;
}) {
  const label = (schema.title || name) + (required ? " *" : "");

  if (isCardChoice(schema)) {
    return (
      <CardChoiceField
        name={name}
        schema={schema}
        label={label}
        value={value}
        onChange={onChange}
        onConfirm={onConfirm}
      />
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

  // Enter in a single-line input confirms (advance/submit) like a native form —
  // textareas keep Enter for newlines, and dropdowns keep it for opening/picking.
  const confirmOnEnter = (event: React.KeyboardEvent) => {
    if (event.key !== "Enter" || event.defaultPrevented) return;
    event.preventDefault();
    onConfirm?.();
  };

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
        onKeyDown={confirmOnEnter}
      />
    );
  } else if (schema.format === "textarea") {
    control = <Textarea value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} rows={3} />;
  } else {
    control = (
      <Input
        type="text"
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={confirmOnEnter}
      />
    );
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
  // Answers start from the schemas' `default`s (#1978): a proposed value IS an answer, so
  // a fully-defaulted form opens with Submit live (confirm-what-I-proposed in one Enter).
  const [values, setValues] = useState<Record<string, unknown>>(() => seedDefaults(steps));
  const [text, setText] = useState("");
  const [current, setCurrent] = useState(0);
  const stepIdx = Math.min(current, Math.max(0, steps.length - 1));
  const rootRef = useRef<HTMLDivElement | null>(null);

  // Hand focus back to wherever it was — the composer, in the chat host — when the card
  // resolves or is dismissed: the counterpart of taking focus on appear. Declared BEFORE
  // the focus-grab effect below: effects run in order, so this one must capture the
  // previously-focused element while it still HAS focus.
  useEffect(() => {
    const prev = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => {
      if (prev?.isConnected) prev.focus();
    };
  }, []);

  // Take focus when the card appears and when the wizard advances a step — never on a
  // mere re-render, so a form popping up mid-keystroke grabs focus once, not per key
  // (#1978). Target: the step's first control in DOM order — within a card group that's
  // the SELECTED card (the roving tabindex parks non-anchors at -1), so a /model- or
  // /effort-style picker opens on its preselected default and bare Enter confirms it.
  // The bare textarea/input fallback covers the free-text card; no step at all
  // (approval) falls back to the first enabled action button.
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const el =
      root.querySelector<HTMLElement>(
        '.hitl-step input, .hitl-step textarea, .hitl-step select, .hitl-step button:not([tabindex="-1"])',
      ) ??
      root.querySelector<HTMLElement>("textarea, input") ??
      root.querySelector<HTMLElement>(".hitl-actions button:not([disabled])");
    el?.focus();
  }, [stepIdx]);

  // Esc anywhere in the card dismisses it — unless a control already used the key (an
  // open dropdown closing itself sets defaultPrevented), and not while busy (mirrors
  // the disabled Dismiss button).
  const onRootKeyDown = (event: React.KeyboardEvent) => {
    if (event.key !== "Escape" || event.defaultPrevented || busy) return;
    event.preventDefault();
    event.stopPropagation();
    onCancel();
  };

  // Approval gate (e.g. run_command) — Approve / Deny on the action.
  if (isApproval) {
    return (
      <div
        className="hitl-card hitl-approval"
        role="dialog"
        aria-label="Approval requested"
        ref={rootRef}
        onKeyDown={onRootKeyDown}
      >
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
      <div className="hitl-card" role="dialog" aria-label="Input requested" ref={rootRef} onKeyDown={onRootKeyDown}>
        <div className="hitl-title">{payload.title || "Input requested"}</div>
        <div className="hitl-prompt">{prompt}</div>
        {/* No autoFocus: it fires at COMMIT (before effects), which would make the
            restore-focus capture above see this textarea instead of the composer.
            The focus-grab effect owns landing here instead. */}
        <Textarea
          className="hitl-freetext"
          value={text}
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
  const step = steps[stepIdx];
  const onLast = stepIdx >= steps.length - 1;
  const stepBlocked = missingInStep(step, values).length > 0; // gates Next on this step
  const submitBlocked = anyStepMissing(steps, values); // gates final Submit across all steps
  const multiStep = steps.length > 1;
  // Enter's action from inside a field — same as the primary button, same gating.
  const confirm = () => {
    if (busy) return;
    if (!onLast) {
      if (!stepBlocked) setCurrent((c) => Math.min(steps.length - 1, c + 1));
    } else if (!submitBlocked) {
      onSubmit(values);
    }
  };

  return (
    <div
      className="hitl-card"
      role="dialog"
      aria-label={payload.title || "Form requested"}
      ref={rootRef}
      onKeyDown={onRootKeyDown}
    >
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
        {visibleFieldsOf(step, values).map(([key, schema, req]) => (
          <Field
            key={key}
            name={key}
            schema={schema}
            required={req}
            value={values[key]}
            onChange={(v) => set(key, v)}
            onConfirm={confirm}
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
