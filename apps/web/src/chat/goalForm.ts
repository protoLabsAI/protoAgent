// Pure logic for the goal-creation form (ADR 0073 completion contracts, Part 2) — the
// HitlPayload the composer/panel renders through the SAME `HitlForm` as `/effort`'s picker,
// plus the answers→`POST /api/goals` body mapping. Kept out of the React/DS layer (no
// `@protolabsai/ui` imports, only a type-only `HitlPayload`) so the web unit suite
// (`src/**/*.test.ts`, no renderer) can exercise the schema shape + the verifier/contract
// mapping directly — mirroring the repo's other pure-helper tests (e.g. shiftCue.ts).

import type { HitlPayload } from "../lib/types";

// The verifier types the operator `/api/goals` surface accepts (ADR 0028/0066). Rendered as
// option cards (the `oneOf` + descriptions turn the field into cards — hitl-form.isCardChoice).
// `llm` is the default (applied in the mapping — HitlForm doesn't seed schema defaults, so the
// field is left OPTIONAL and an unpicked card falls back to llm rather than blocking Submit).
export const GOAL_VERIFIER_TYPES = [
  { value: "command", label: "command", description: "A shell command that exits 0" },
  { value: "test", label: "test", description: "A test command that exits 0" },
  { value: "ci", label: "ci", description: "GitHub checks are green (PR # or branch)" },
  { value: "data", label: "data", description: "Assert over a file's contents" },
  { value: "llm", label: "llm", description: "Fuzzy LLM judgment (the default)" },
] as const;

export const DEFAULT_MAX_ITERATIONS = 8;

// A single-step HITL form: the goal + how to verify it, then the OPTIONAL completion
// contract (outcome/constraints/boundaries/stop_when/max iterations). Single-step (not a
// wizard) so `condition` — the one required field — gates Submit directly, and both hosts
// (the chat `/goal new` composer form and the GoalsPanel inline form) render it identically.
export function goalFormPayload(): HitlPayload {
  return {
    kind: "form",
    title: "New goal",
    description: "A testable outcome the agent self-drives toward. The verifier decides DONE.",
    steps: [
      {
        schema: {
          type: "object",
          required: ["condition"],
          properties: {
            condition: {
              type: "string",
              title: "Goal",
              description: "The outcome the agent should achieve.",
            },
            verifier: {
              type: "string",
              title: "How to verify",
              default: "llm",
              oneOf: GOAL_VERIFIER_TYPES.map((v) => ({
                const: v.value,
                title: v.label,
                description: v.description,
              })),
            },
            verification: {
              type: "string",
              title: "Verification detail",
              description:
                "The verifier's parameter: the shell command (command/test), the PR # or " +
                "branch (ci), or `path :: substring` (data). Leave blank for llm.",
            },
            outcome: {
              type: "string",
              title: "Outcome (optional)",
              description: "The required end-state, in one line. Defaults to the goal.",
            },
            constraints: {
              type: "string",
              format: "textarea",
              title: "Constraints (optional)",
              description: "Invariants the agent must NOT violate — one per line.",
            },
            boundaries: {
              type: "string",
              format: "textarea",
              title: "Boundaries (optional)",
              description: "Files / dirs / systems in scope — one per line.",
            },
            stop_when: {
              type: "string",
              title: "Stop and ask when (optional)",
              description: "A condition under which the agent pauses and asks you.",
            },
            max_iterations: {
              type: "number",
              title: "Max iterations (optional)",
              default: DEFAULT_MAX_ITERATIONS,
              description: `Drive-loop budget. Default ${DEFAULT_MAX_ITERATIONS}.`,
            },
          },
        },
      },
    ],
  };
}

// The `POST /api/goals` body (operator goal-set, ADR 0066/0073). `verifier` is an opaque
// dict the backend validates; the contract fields are omitted when empty so a contract-less
// goal is byte-for-byte the pre-0073 shape.
export type GoalSetBody = {
  session_id: string;
  condition: string;
  verifier: Record<string, unknown>;
  outcome?: string;
  constraints?: string[];
  boundaries?: string[];
  stop_when?: string;
  max_iterations?: number;
};

/** Split a textarea value into trimmed, non-empty lines → `string[]` (constraints/boundaries
 *  are one-per-line). Mirrors the backend's `_as_str_list` coercion (blank entries dropped). */
export function splitLines(value: unknown): string[] {
  return String(value ?? "")
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Assemble the verifier dict from the picked type + its free-text detail (ADR 0028 shapes):
 *  - command / test → `{type, command: detail}` (detail omitted when blank)
 *  - ci             → `{type:"ci", pr:<n>}` when detail is `#123`/`123`, else `{type:"ci", branch: detail}`
 *  - data           → `{type:"data", path, contains}` parsed from `path :: substring`
 *                     (best-effort; a bare value is taken as the path)
 *  - llm / unknown  → `{type:"llm"}` (the default) */
export function buildVerifier(type: unknown, detail: unknown): Record<string, unknown> {
  const t = String(type ?? "").trim().toLowerCase() || "llm";
  const d = String(detail ?? "").trim();

  if (t === "command" || t === "test") {
    return d ? { type: t, command: d } : { type: t };
  }
  if (t === "ci") {
    const pr = /^#?(\d+)$/.exec(d);
    if (pr) return { type: "ci", pr: Number(pr[1]) };
    return d ? { type: "ci", branch: d } : { type: "ci" };
  }
  if (t === "data") {
    const sep = d.indexOf("::");
    const path = (sep >= 0 ? d.slice(0, sep) : d).trim();
    const contains = sep >= 0 ? d.slice(sep + 2).trim() : "";
    const spec: Record<string, unknown> = { type: "data" };
    if (path) spec.path = path;
    if (contains) spec.contains = contains;
    return spec;
  }
  return { type: "llm" };
}

/** Coerce the (optional) max-iterations answer to a positive integer, defaulting to
 *  `DEFAULT_MAX_ITERATIONS` when blank / non-numeric / ≤ 0. */
export function parseMaxIterations(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : DEFAULT_MAX_ITERATIONS;
}

/** Map the HitlForm answers → the `POST /api/goals` body for `session_id`. Returns `null`
 *  when there is no condition (the one required field) so callers can no-op rather than POST
 *  an unsatisfiable goal. Empty contract fields are omitted (backward-compatible). */
export function buildGoalSetBody(
  sessionId: string,
  answers: Record<string, unknown>,
): GoalSetBody | null {
  const condition = String(answers.condition ?? "").trim();
  if (!condition) return null;

  const verifier = buildVerifier(answers.verifier, answers.verification);
  const outcome = String(answers.outcome ?? "").trim();
  const constraints = splitLines(answers.constraints);
  const boundaries = splitLines(answers.boundaries);
  const stop_when = String(answers.stop_when ?? "").trim();

  return {
    session_id: sessionId,
    condition,
    verifier,
    ...(outcome ? { outcome } : {}),
    ...(constraints.length ? { constraints } : {}),
    ...(boundaries.length ? { boundaries } : {}),
    ...(stop_when ? { stop_when } : {}),
    max_iterations: parseMaxIterations(answers.max_iterations),
  };
}
