import { describe, expect, it } from "vitest";

import {
  DEFAULT_MAX_ITERATIONS,
  GOAL_VERIFIER_TYPES,
  buildGoalSetBody,
  buildVerifier,
  goalFormPayload,
  parseMaxIterations,
  splitLines,
  verifierDetail,
} from "./goalForm";

// The form is rendered by HitlForm (not exercised here — jsdom has no renderer and the DS
// forms are a private-registry dep). These tests pin the PURE pieces: the two-step wizard's
// schema shape (incl. the type-aware `showWhen` verification fields) + the answers→
// `POST /api/goals` body mapping (verifier assembly, line-splitting).

type Props = Record<string, { type?: string; oneOf?: { const?: unknown }[]; format?: string; showWhen?: unknown }>;

describe("goalFormPayload", () => {
  const payload = goalFormPayload();
  const step1 = payload.steps?.[0]?.schema as { required?: string[]; properties?: Props };
  const step2 = payload.steps?.[1]?.schema as { required?: string[]; properties?: Props };

  it("is a two-step wizard with a title", () => {
    expect(payload.kind).toBe("form");
    expect(payload.steps).toHaveLength(2);
    expect(payload.title).toBeTruthy();
  });

  it("step 1 requires only the condition", () => {
    expect(step1.required).toEqual(["condition"]);
  });

  it("step 1 holds the goal, the verifier cards, and the type-aware verification fields", () => {
    const props = step1.properties ?? {};
    for (const key of ["condition", "verifier", "verify_command", "verify_ci", "verify_data_path", "verify_data_contains"]) {
      expect(props[key], `missing step-1 field ${key}`).toBeDefined();
    }
  });

  it("step 2 holds the (optional) completion-contract fields", () => {
    const props = step2.properties ?? {};
    for (const key of ["outcome", "constraints", "boundaries", "stop_when", "max_iterations"]) {
      expect(props[key], `missing step-2 field ${key}`).toBeDefined();
    }
    expect(step2.required ?? []).toEqual([]); // the whole contract is optional
  });

  it("renders the verifier field as option cards for every type (single source of truth)", () => {
    const cards = step1.properties?.verifier?.oneOf ?? [];
    expect(cards.map((c) => c.const)).toEqual(GOAL_VERIFIER_TYPES.map((v) => v.value));
  });

  it("gates each verification field on the matching verifier (showWhen)", () => {
    const p = step1.properties ?? {};
    expect(p.verify_command?.showWhen).toEqual({ field: "verifier", in: ["command", "test"] });
    expect(p.verify_ci?.showWhen).toEqual({ field: "verifier", equals: "ci" });
    expect(p.verify_data_path?.showWhen).toEqual({ field: "verifier", equals: "data" });
    expect(p.verify_data_contains?.showWhen).toEqual({ field: "verifier", equals: "data" });
  });

  it("multi-line contract fields render as textareas", () => {
    expect(step2.properties?.constraints?.format).toBe("textarea");
    expect(step2.properties?.boundaries?.format).toBe("textarea");
  });
});

describe("verifierDetail — reads the type-aware field(s) for the picked verifier", () => {
  it("command / test → the shell-command field", () => {
    expect(verifierDetail({ verifier: "command", verify_command: "pytest -q" })).toBe("pytest -q");
    expect(verifierDetail({ verifier: "test", verify_command: "npm test" })).toBe("npm test");
  });

  it("ci → the PR#/branch field", () => {
    expect(verifierDetail({ verifier: "ci", verify_ci: "#42" })).toBe("#42");
  });

  it("data → `path :: substring`, or a bare path when the substring is blank", () => {
    expect(verifierDetail({ verifier: "data", verify_data_path: "out.json", verify_data_contains: "ready" })).toBe(
      "out.json :: ready",
    );
    expect(verifierDetail({ verifier: "data", verify_data_path: "out.json" })).toBe("out.json");
  });

  it("llm (and anything else) → empty", () => {
    expect(verifierDetail({ verifier: "llm" })).toBe("");
    expect(verifierDetail({})).toBe("");
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
    expect(splitLines("a\r\n\n  b  \nc\n")).toEqual(["a", "b", "c"]);
    expect(splitLines("")).toEqual([]);
    expect(splitLines(null)).toEqual([]);
  });
});

describe("parseMaxIterations", () => {
  it("defaults blank / non-numeric / ≤0 to the default", () => {
    expect(parseMaxIterations("")).toBe(DEFAULT_MAX_ITERATIONS);
    expect(parseMaxIterations("x")).toBe(DEFAULT_MAX_ITERATIONS);
    expect(parseMaxIterations(0)).toBe(DEFAULT_MAX_ITERATIONS);
    expect(parseMaxIterations(-3)).toBe(DEFAULT_MAX_ITERATIONS);
  });

  it("floors a positive number", () => {
    expect(parseMaxIterations(12)).toBe(12);
    expect(parseMaxIterations("7")).toBe(7);
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

  it("assembles the verifier from the type-aware field and splits the contract lists", () => {
    const body = buildGoalSetBody("s2", {
      condition: "suite green",
      verifier: "command",
      verify_command: "pytest -q",
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

  it("assembles a data verifier from its two type-aware fields", () => {
    const body = buildGoalSetBody("sd", {
      condition: "deployed",
      verifier: "data",
      verify_data_path: "status.json",
      verify_data_contains: "ok",
    });
    expect(body?.verifier).toEqual({ type: "data", path: "status.json", contains: "ok" });
  });

  it("omits empty contract fields (backward-compatible shape)", () => {
    const body = buildGoalSetBody("s3", {
      condition: "done",
      verifier: "ci",
      verify_ci: "#42",
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
