import { describe, expect, it } from "vitest";

import {
  DEFAULT_MAX_ITERATIONS,
  GOAL_VERIFIER_TYPES,
  buildGoalSetBody,
  buildVerifier,
  goalFormPayload,
  parseMaxIterations,
  splitLines,
} from "./goalForm";

// The form is rendered by HitlForm (not exercised here — jsdom has no renderer and the DS
// forms are a private-registry dep). These tests pin the PURE pieces: the payload's schema
// shape + the answers→`POST /api/goals` body mapping (verifier assembly, line-splitting).

describe("goalFormPayload", () => {
  const payload = goalFormPayload();
  const step = payload.steps?.[0];
  const schema = step?.schema as {
    required?: string[];
    properties?: Record<string, { type?: string; oneOf?: { const?: unknown }[]; format?: string }>;
  };

  it("is a single-step form with a title", () => {
    expect(payload.kind).toBe("form");
    expect(payload.steps).toHaveLength(1);
    expect(payload.title).toBeTruthy();
  });

  it("requires only the condition", () => {
    expect(schema.required).toEqual(["condition"]);
  });

  it("declares every contract field", () => {
    const props = schema.properties ?? {};
    for (const key of [
      "condition",
      "verifier",
      "verification",
      "outcome",
      "constraints",
      "boundaries",
      "stop_when",
      "max_iterations",
    ]) {
      expect(props[key], `missing field ${key}`).toBeDefined();
    }
  });

  it("renders the verifier field as option cards for every type", () => {
    const cards = schema.properties?.verifier?.oneOf ?? [];
    expect(cards.map((c) => c.const)).toEqual(["command", "test", "ci", "data", "llm"]);
    // The card set mirrors the exported constant (single source of truth).
    expect(cards.map((c) => c.const)).toEqual(GOAL_VERIFIER_TYPES.map((v) => v.value));
  });

  it("multi-line contract fields render as textareas", () => {
    expect(schema.properties?.constraints?.format).toBe("textarea");
    expect(schema.properties?.boundaries?.format).toBe("textarea");
  });
});

describe("buildVerifier", () => {
  it("command / test carry the detail as the shell command", () => {
    expect(buildVerifier("command", "pytest -q")).toEqual({ type: "command", command: "pytest -q" });
    expect(buildVerifier("test", "npm test")).toEqual({ type: "test", command: "npm test" });
  });

  it("command / test with no detail omit the command", () => {
    expect(buildVerifier("command", "")).toEqual({ type: "command" });
  });

  it("ci detects a PR number (with or without #) vs a branch name", () => {
    expect(buildVerifier("ci", "#123")).toEqual({ type: "ci", pr: 123 });
    expect(buildVerifier("ci", "123")).toEqual({ type: "ci", pr: 123 });
    expect(buildVerifier("ci", "feat/my-branch")).toEqual({ type: "ci", branch: "feat/my-branch" });
    expect(buildVerifier("ci", "")).toEqual({ type: "ci" });
  });

  it("data parses `path :: substring`, falling back to a bare path", () => {
    expect(buildVerifier("data", "out.json :: ready")).toEqual({
      type: "data",
      path: "out.json",
      contains: "ready",
    });
    expect(buildVerifier("data", "out.json")).toEqual({ type: "data", path: "out.json" });
  });

  it("llm and unknown types collapse to {type:'llm'}", () => {
    expect(buildVerifier("llm", "")).toEqual({ type: "llm" });
    expect(buildVerifier("", "")).toEqual({ type: "llm" });
    expect(buildVerifier("nonsense", "x")).toEqual({ type: "llm" });
  });
});

describe("splitLines", () => {
  it("trims, drops blanks, and handles CRLF", () => {
    expect(splitLines("a\nb\r\n\n  c  ")).toEqual(["a", "b", "c"]);
    expect(splitLines("")).toEqual([]);
    expect(splitLines(undefined)).toEqual([]);
  });
});

describe("parseMaxIterations", () => {
  it("defaults blank / non-numeric / ≤0 to the default", () => {
    expect(parseMaxIterations("")).toBe(DEFAULT_MAX_ITERATIONS);
    expect(parseMaxIterations(undefined)).toBe(DEFAULT_MAX_ITERATIONS);
    expect(parseMaxIterations(0)).toBe(DEFAULT_MAX_ITERATIONS);
    expect(parseMaxIterations(-3)).toBe(DEFAULT_MAX_ITERATIONS);
  });
  it("floors a positive number", () => {
    expect(parseMaxIterations(12)).toBe(12);
    expect(parseMaxIterations("5")).toBe(5);
    expect(parseMaxIterations(4.9)).toBe(4);
  });
});

describe("buildGoalSetBody", () => {
  it("returns null without a condition", () => {
    expect(buildGoalSetBody("operator", {})).toBeNull();
    expect(buildGoalSetBody("operator", { condition: "   " })).toBeNull();
  });

  it("maps a minimal goal (default llm verifier, default max_iterations)", () => {
    expect(buildGoalSetBody("s1", { condition: "ship it" })).toEqual({
      session_id: "s1",
      condition: "ship it",
      verifier: { type: "llm" },
      max_iterations: DEFAULT_MAX_ITERATIONS,
    });
  });

  it("assembles the verifier from type + detail and splits the contract lists", () => {
    const body = buildGoalSetBody("s2", {
      condition: "suite green",
      verifier: "command",
      verification: "pytest -q",
      outcome: "the suite is green on main",
      constraints: "no new network calls\npublic API unchanged",
      boundaries: "graph/goals/\ntests/",
      stop_when: "a schema migration is needed",
      max_iterations: 20,
    });
    expect(body).toEqual({
      session_id: "s2",
      condition: "suite green",
      verifier: { type: "command", command: "pytest -q" },
      outcome: "the suite is green on main",
      constraints: ["no new network calls", "public API unchanged"],
      boundaries: ["graph/goals/", "tests/"],
      stop_when: "a schema migration is needed",
      max_iterations: 20,
    });
  });

  it("omits empty contract fields (backward-compatible shape)", () => {
    const body = buildGoalSetBody("s3", {
      condition: "done",
      verifier: "ci",
      verification: "#42",
      constraints: "\n  \n",
    });
    expect(body).toEqual({
      session_id: "s3",
      condition: "done",
      verifier: { type: "ci", pr: 42 },
      max_iterations: DEFAULT_MAX_ITERATIONS,
    });
    expect(body).not.toHaveProperty("constraints");
    expect(body).not.toHaveProperty("outcome");
  });
});
