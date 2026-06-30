// Pure logic for the HITL wizard (request_user_input) — kept out of HitlForm.tsx so it can
// be unit-tested (the web suite runs `src/**/*.test.ts`, no React renderer). Covers reading a
// step's fields, normalizing choice options (AskUserQuestion-style cards), and per-step
// required-field validation that gates Back/Next/Submit.

import type { HitlFormStep } from "../lib/types";

export type FieldSchema = {
  type?: string;
  title?: string;
  description?: string;
  enum?: unknown[];
  // JSON-schema-idiomatic labelled options (rjsf convention): each is a value + human label
  // + optional description. Presence of `oneOf` is what turns a field into option cards.
  oneOf?: Array<{ const?: unknown; title?: string; description?: string }>;
  items?: FieldSchema; // element schema for a multi-select (`type: "array"`)
  format?: string;
  default?: unknown;
  // Opt a bare `enum` into card rendering (otherwise an enum stays a dropdown).
  "x-display"?: string;
};

export type FieldEntry = [key: string, schema: FieldSchema, required: boolean];

export type ChoiceOption = { value: string; label: string; description?: string };

/** `[key, schema, required]` for every property declared in a step's JSON schema. */
export function fieldsOf(step: HitlFormStep | undefined): FieldEntry[] {
  const schema = (step?.schema || {}) as {
    properties?: Record<string, FieldSchema>;
    required?: string[];
  };
  const required = new Set(schema.required || []);
  return Object.entries(schema.properties || {}).map(([key, fs]) => [key, fs, required.has(key)]);
}

/** A multi-select choice is a JSON-schema array; its options live on `items`. */
export function isMultiChoice(schema: FieldSchema): boolean {
  return schema?.type === "array";
}

/** The schema that actually carries the options (the element schema for multi-select). */
function optionSource(schema: FieldSchema): FieldSchema {
  return isMultiChoice(schema) ? (schema.items as FieldSchema) || {} : schema;
}

/** Normalized option cards from `oneOf` [{const,title,description}] (preferred, carries
 *  descriptions) or a plain `enum` (label only). Empty when the field isn't a choice. */
export function optionsOf(schema: FieldSchema): ChoiceOption[] {
  const src = optionSource(schema);
  if (Array.isArray(src?.oneOf)) {
    return src.oneOf.map((o) => ({
      value: String(o.const ?? o.title ?? ""),
      label: String(o.title ?? o.const ?? ""),
      description: o.description ? String(o.description) : undefined,
    }));
  }
  if (Array.isArray(src?.enum)) {
    return src.enum.map((v) => ({ value: String(v), label: String(v) }));
  }
  return [];
}

/** Render this field as option cards rather than a dropdown/input? Cards when it has
 *  rich `oneOf` options, is explicitly `x-display: "cards"`, or is any multi-select
 *  (there's no native multi-dropdown). A bare single-select `enum` stays a dropdown. */
export function isCardChoice(schema: FieldSchema): boolean {
  const src = optionSource(schema);
  if (isMultiChoice(schema)) return Array.isArray(src?.oneOf) || Array.isArray(src?.enum);
  return Array.isArray(src?.oneOf) || schema?.["x-display"] === "cards";
}

/** Is a single field's value present (for required-gating)? A boolean is always answered
 *  (unchecked = a valid `false`); a multi-select needs ≥1 selection; everything else needs
 *  a non-empty value. */
export function hasValue(schema: FieldSchema, value: unknown): boolean {
  if (isMultiChoice(schema)) return Array.isArray(value) && value.length > 0;
  if (schema?.type === "boolean") return true;
  return value !== undefined && value !== null && value !== "";
}

/** Required field keys in this step that are still empty — non-empty ⇒ block Next/Submit. */
export function missingInStep(step: HitlFormStep | undefined, values: Record<string, unknown>): string[] {
  return fieldsOf(step)
    .filter(([key, schema, required]) => required && !hasValue(schema, values[key]))
    .map(([key]) => key);
}

/** Any required field empty across ALL steps — gates the final Submit. */
export function anyStepMissing(steps: HitlFormStep[], values: Record<string, unknown>): boolean {
  return (steps || []).some((s) => missingInStep(s, values).length > 0);
}
