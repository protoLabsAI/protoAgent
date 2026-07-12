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
  // Conditional visibility (mirrors the settings `depends_on` convention): this field
  // renders only when a sibling field's answer matches. `equals` = strict equality; `in` =
  // membership; neither = the sibling is truthy. A hidden field is skipped by required-
  // gating too, so an optional-when-hidden field never blocks Next/Submit.
  showWhen?: { field: string; equals?: unknown; in?: unknown[] };
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

/** Should this field render, given the current answers? A field with no `showWhen` always
 *  shows; otherwise its condition must match the sibling field's value. */
export function isFieldVisible(schema: FieldSchema, values: Record<string, unknown>): boolean {
  const cond = schema?.showWhen;
  if (!cond || !cond.field) return true;
  const v = values[cond.field];
  if (Array.isArray(cond.in)) return cond.in.some((x) => x === v);
  if (cond.equals !== undefined) return v === cond.equals;
  return Boolean(v); // showWhen with neither equals nor in ⇒ sibling is truthy
}

/** The step's fields that are visible under the current answers (drops `showWhen`-hidden). */
export function visibleFieldsOf(step: HitlFormStep | undefined, values: Record<string, unknown>): FieldEntry[] {
  return fieldsOf(step).filter(([, schema]) => isFieldVisible(schema, values));
}

/** Initial answers seeded from each field's schema `default`, across ALL steps up front
 *  (not just the visible one) so `anyStepMissing` reflects the prefill immediately.
 *  Contract (#1978): a `default` IS an answer — a required field that carries one arrives
 *  satisfied, so a fully-defaulted form opens with Submit live and confirms untouched.
 *  Producers only set `default` on a real proposal (the tab's current model/effort, an
 *  agent's suggested value), so confirm-as-is is the point. `false`/`0` are kept — only
 *  an absent (`undefined`) default leaves a field unanswered. */
export function seedDefaults(steps: HitlFormStep[]): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  for (const step of steps || [])
    for (const [key, schema] of fieldsOf(step)) if (schema.default !== undefined) values[key] = schema.default;
  return values;
}

/** Required field keys in this step that are still empty — non-empty ⇒ block Next/Submit.
 *  A `showWhen`-hidden field is never "missing" (it isn't asked, so it can't gate). */
export function missingInStep(step: HitlFormStep | undefined, values: Record<string, unknown>): string[] {
  return fieldsOf(step)
    .filter(([key, schema, required]) => required && isFieldVisible(schema, values) && !hasValue(schema, values[key]))
    .map(([key]) => key);
}

/** Any required field empty across ALL steps — gates the final Submit. */
export function anyStepMissing(steps: HitlFormStep[], values: Record<string, unknown>): boolean {
  return (steps || []).some((s) => missingInStep(s, values).length > 0);
}
