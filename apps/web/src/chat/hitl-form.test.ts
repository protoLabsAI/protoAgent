import { describe, expect, it } from "vitest";

import type { HitlFormStep } from "../lib/types";
import {
  anyStepMissing,
  fieldsOf,
  hasValue,
  isCardChoice,
  isFieldVisible,
  isMultiChoice,
  missingInStep,
  optionsOf,
  seedDefaults,
  visibleFieldsOf,
} from "./hitl-form";

const step = (schema: Record<string, unknown>): HitlFormStep => ({ schema });

describe("fieldsOf", () => {
  it("returns [key, schema, required] for each property", () => {
    const s = step({
      properties: { env: { type: "string" }, note: { type: "string" } },
      required: ["env"],
    });
    expect(fieldsOf(s)).toEqual([
      ["env", { type: "string" }, true],
      ["note", { type: "string" }, false],
    ]);
  });

  it("is empty for an undefined step or a step with no properties", () => {
    expect(fieldsOf(undefined)).toEqual([]);
    expect(fieldsOf(step({}))).toEqual([]);
  });
});

describe("optionsOf", () => {
  it("normalizes rich oneOf options (value/label/description)", () => {
    const schema = {
      type: "string",
      oneOf: [
        { const: "pg", title: "Postgres", description: "Durable" },
        { const: "sqlite", title: "SQLite" },
      ],
    };
    expect(optionsOf(schema)).toEqual([
      { value: "pg", label: "Postgres", description: "Durable" },
      { value: "sqlite", label: "SQLite", description: undefined },
    ]);
  });

  it("falls back to a plain enum (label === value, no description)", () => {
    expect(optionsOf({ type: "string", enum: ["a", "b"] })).toEqual([
      { value: "a", label: "a" },
      { value: "b", label: "b" },
    ]);
  });

  it("reads options off `items` for a multi-select array", () => {
    const schema = { type: "array", items: { oneOf: [{ const: "x", title: "X" }] } };
    expect(optionsOf(schema)).toEqual([{ value: "x", label: "X", description: undefined }]);
  });
});

describe("isCardChoice / isMultiChoice", () => {
  it("treats oneOf and x-display:cards as cards, but a bare single enum as a dropdown", () => {
    expect(isCardChoice({ type: "string", oneOf: [{ const: "a" }] })).toBe(true);
    expect(isCardChoice({ type: "string", enum: ["a"], "x-display": "cards" })).toBe(true);
    expect(isCardChoice({ type: "string", enum: ["a"] })).toBe(false); // dropdown
    expect(isCardChoice({ type: "string" })).toBe(false);
  });

  it("renders any multi-select (array) of options as cards", () => {
    expect(isMultiChoice({ type: "array" })).toBe(true);
    expect(isCardChoice({ type: "array", items: { enum: ["a", "b"] } })).toBe(true);
  });
});

describe("hasValue", () => {
  it("booleans are always answered (unchecked = a valid false)", () => {
    expect(hasValue({ type: "boolean" }, undefined)).toBe(true);
    expect(hasValue({ type: "boolean" }, false)).toBe(true);
  });

  it("multi-select needs at least one selection", () => {
    expect(hasValue({ type: "array" }, [])).toBe(false);
    expect(hasValue({ type: "array" }, ["a"])).toBe(true);
  });

  it("scalar fields need a non-empty value", () => {
    expect(hasValue({ type: "string" }, "")).toBe(false);
    expect(hasValue({ type: "string" }, undefined)).toBe(false);
    expect(hasValue({ type: "string" }, "x")).toBe(true);
    expect(hasValue({ type: "number" }, 0)).toBe(true); // 0 is a real answer
  });
});

describe("missingInStep / anyStepMissing", () => {
  const s1 = step({ properties: { env: { type: "string" } }, required: ["env"] });
  const s2 = step({ properties: { region: { type: "array" } }, required: ["region"] });

  it("reports required fields that are still empty in a step", () => {
    expect(missingInStep(s1, {})).toEqual(["env"]);
    expect(missingInStep(s1, { env: "prod" })).toEqual([]);
  });

  it("ignores empty optional fields", () => {
    const opt = step({ properties: { note: { type: "string" } } });
    expect(missingInStep(opt, {})).toEqual([]);
  });

  it("gates the final submit until every step is satisfied", () => {
    expect(anyStepMissing([s1, s2], { env: "prod" })).toBe(true); // region still missing
    expect(anyStepMissing([s1, s2], { env: "prod", region: ["eu"] })).toBe(false);
  });

  it("a `showWhen`-HIDDEN required field never gates (it isn't asked)", () => {
    const conditional = step({
      properties: {
        mode: { type: "string" },
        detail: { type: "string", showWhen: { field: "mode", equals: "custom" } },
      },
      required: ["detail"],
    });
    // mode≠custom → `detail` is hidden → not missing, so Submit isn't blocked.
    expect(missingInStep(conditional, { mode: "auto" })).toEqual([]);
    // mode=custom → `detail` shows → now required-and-empty blocks.
    expect(missingInStep(conditional, { mode: "custom" })).toEqual(["detail"]);
    expect(missingInStep(conditional, { mode: "custom", detail: "x" })).toEqual([]);
  });
});

describe("seedDefaults — schema defaults prefill the answers (#1978)", () => {
  it("seeds every field carrying a default, across ALL steps up front", () => {
    const s1 = step({
      properties: {
        env: { type: "string", default: "staging" },
        note: { type: "string" }, // no default — stays unanswered
      },
    });
    const s2 = step({ properties: { strategy: { type: "string", default: "rolling" } } });
    expect(seedDefaults([s1, s2])).toEqual({ env: "staging", strategy: "rolling" });
  });

  it("keeps falsy defaults (false / 0 / \"\") — only an absent default is skipped", () => {
    const s = step({
      properties: {
        dry_run: { type: "boolean", default: false },
        retries: { type: "number", default: 0 },
        prefix: { type: "string", default: "" },
      },
    });
    expect(seedDefaults([s])).toEqual({ dry_run: false, retries: 0, prefix: "" });
  });

  it("is empty for no steps or steps without defaults", () => {
    expect(seedDefaults([])).toEqual({});
    expect(seedDefaults([step({ properties: { env: { type: "string" } } })])).toEqual({});
  });

  it("a required field's default satisfies the gate — Submit is live untouched", () => {
    const s = step({
      properties: { model: { type: "string", default: "protolabs/fast", oneOf: [{ const: "protolabs/fast" }] } },
      required: ["model"],
    });
    const seeded = seedDefaults([s]);
    expect(missingInStep(s, seeded)).toEqual([]);
    expect(anyStepMissing([s], seeded)).toBe(false);
  });

  it("a required field WITHOUT a default still gates as before", () => {
    const s = step({
      properties: { env: { type: "string" }, mode: { type: "string", default: "auto" } },
      required: ["env", "mode"],
    });
    expect(missingInStep(s, seedDefaults([s]))).toEqual(["env"]);
  });
});

describe("isFieldVisible / visibleFieldsOf — conditional fields (showWhen)", () => {
  it("no showWhen ⇒ always visible", () => {
    expect(isFieldVisible({ type: "string" }, {})).toBe(true);
  });

  it("`equals` shows only on a strict match", () => {
    const f = { type: "string", showWhen: { field: "kind", equals: "ci" } };
    expect(isFieldVisible(f, { kind: "ci" })).toBe(true);
    expect(isFieldVisible(f, { kind: "command" })).toBe(false);
    expect(isFieldVisible(f, {})).toBe(false);
  });

  it("`in` shows on membership", () => {
    const f = { type: "string", showWhen: { field: "kind", in: ["command", "test"] } };
    expect(isFieldVisible(f, { kind: "test" })).toBe(true);
    expect(isFieldVisible(f, { kind: "data" })).toBe(false);
  });

  it("showWhen without equals/in ⇒ sibling must be truthy", () => {
    const f = { type: "string", showWhen: { field: "on" } };
    expect(isFieldVisible(f, { on: true })).toBe(true);
    expect(isFieldVisible(f, { on: "" })).toBe(false);
  });

  it("visibleFieldsOf drops the hidden fields for the current answers", () => {
    const s = step({
      properties: {
        kind: { type: "string" },
        cmd: { type: "string", showWhen: { field: "kind", equals: "command" } },
        pr: { type: "string", showWhen: { field: "kind", equals: "ci" } },
      },
    });
    expect(visibleFieldsOf(s, { kind: "command" }).map(([k]) => k)).toEqual(["kind", "cmd"]);
    expect(visibleFieldsOf(s, { kind: "ci" }).map(([k]) => k)).toEqual(["kind", "pr"]);
  });
});
