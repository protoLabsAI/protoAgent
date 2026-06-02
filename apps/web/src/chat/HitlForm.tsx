import { useState } from "react";

import type { HitlFormStep, HitlPayload } from "../lib/types";

// A lightweight JSON-schema form for HITL requests (request_user_input) and a
// plain prompt for ask_human. Renders the common field types
// (string/number/boolean/enum/textarea) — not a full @rjsf, but enough for the
// config/choice/approval forms agents actually ask for. Multi-step = a single
// scrollable form (all steps collected, submitted together).

type FieldSchema = {
  type?: string;
  title?: string;
  description?: string;
  enum?: unknown[];
  format?: string;
  default?: unknown;
};

function fieldsOf(step: HitlFormStep): Array<[string, FieldSchema, boolean]> {
  const schema = (step.schema || {}) as {
    properties?: Record<string, FieldSchema>;
    required?: string[];
  };
  const required = new Set(schema.required || []);
  return Object.entries(schema.properties || {}).map(([key, fs]) => [key, fs, required.has(key)]);
}

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

  if (schema.type === "boolean") {
    return (
      <label className="hitl-field hitl-field-bool">
        <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
        <span>{label}</span>
      </label>
    );
  }

  let control;
  if (Array.isArray(schema.enum)) {
    control = (
      <select value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
        <option value="" disabled>
          Select…
        </option>
        {schema.enum.map((opt) => (
          <option key={String(opt)} value={String(opt)}>
            {String(opt)}
          </option>
        ))}
      </select>
    );
  } else if (schema.type === "number" || schema.type === "integer") {
    control = (
      <input
        type="number"
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
      />
    );
  } else if (schema.format === "textarea") {
    control = <textarea value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} rows={3} />;
  } else {
    control = <input type="text" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />;
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
}: {
  payload: HitlPayload;
  busy?: boolean;
  onSubmit: (response: Record<string, unknown> | string) => void;
  onCancel: () => void;
}) {
  const isForm = payload.kind === "form" && (payload.steps?.length ?? 0) > 0;
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [text, setText] = useState("");

  // ask_human / free-text question.
  if (!isForm) {
    const prompt = payload.question || payload.description || payload.title || "Input requested.";
    return (
      <div className="hitl-card" role="dialog" aria-label="Input requested">
        <div className="hitl-title">{payload.title || "Input requested"}</div>
        <div className="hitl-prompt">{prompt}</div>
        <textarea
          className="hitl-freetext"
          value={text}
          autoFocus
          placeholder="Your answer…"
          onChange={(e) => setText(e.target.value)}
        />
        <div className="hitl-actions">
          <button type="button" className="ghost-button" onClick={onCancel} disabled={busy}>
            Dismiss
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={() => onSubmit(text.trim())}
            disabled={busy || !text.trim()}
          >
            Send
          </button>
        </div>
      </div>
    );
  }

  const set = (k: string, v: unknown) => setValues((prev) => ({ ...prev, [k]: v }));
  // Required fields across all steps must be filled before submit.
  const missing = (payload.steps || []).some((step) =>
    fieldsOf(step).some(([key, , req]) => req && (values[key] === undefined || values[key] === "")),
  );

  return (
    <div className="hitl-card" role="dialog" aria-label={payload.title || "Form requested"}>
      <div className="hitl-title">{payload.title || "Input requested"}</div>
      {payload.description && <div className="hitl-prompt">{payload.description}</div>}
      {(payload.steps || []).map((step, i) => (
        <div className="hitl-step" key={i}>
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
      ))}
      <div className="hitl-actions">
        <button type="button" className="ghost-button" onClick={onCancel} disabled={busy}>
          Dismiss
        </button>
        <button
          type="button"
          className="primary-button"
          onClick={() => onSubmit(values)}
          disabled={busy || missing}
        >
          Submit
        </button>
      </div>
    </div>
  );
}
