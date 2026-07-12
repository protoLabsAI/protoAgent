// Pure logic for the goal-creation form (ADR 0073 completion contracts, Part 2) — the
// HitlPayload the composer/panel renders through the SAME `HitlForm` as `/effort`'s picker,
// plus the answers→`POST /api/goals` body mapping. Kept out of the React/DS layer (no
// `@protolabsai/ui` imports, only a type-only `HitlPayload`) so the web unit suite
// (`src/**/*.test.ts`, no renderer) can exercise the schema shape + the verifier/contract
// mapping directly — mirroring the repo's other pure-helper tests (e.g. shiftCue.ts).

import type { HitlPayload } from "../lib/types";

// The verifier types the operator `/api/goals` surface accepts (ADR 0028/0066). Rendered as
// option cards (the `oneOf` + descriptions turn the field into cards — hitl-form.isCardChoice).
// `llm` is the default: HitlForm seeds it from the schema (#1978) so the card opens selected,
// and the mapping still coerces an absent/unknown answer to llm as the belt-and-braces layer.
export const GOAL_VERIFIER_TYPES = [
  { value: "command", label: "command", description: "A shell command that exits 0" },
  { value: "test", label: "test", description: "A test command that exits 0" },
  { value: "ci", label: "ci", description: "GitHub checks are green (PR # or branch)" },
  { value: "data", label: "data", description: "Assert over a file's contents" },
  { value: "llm", label: "llm", description: "Fuzzy LLM judgment (the default)" },
] as const;

export const DEFAULT_MAX_ITERATIONS = 8;

// A two-step wizard (ADR 0073). Step 1 = the goal + how to verify it, with a TYPE-AWARE
// verification input: the verifier cards drive `showWhen`-conditional fields, so only the
// input(s) the picked verifier actually needs are shown (a shell command for command/test,
// a PR#/branch for ci, a file+substring for data, nothing for llm) — no catch-all box.
// Step 2 = the OPTIONAL completion contract. `condition` (the one required field) lives in
// step 1 and gates its Next. Both hosts (the `/goal new` composer form and the GoalsPanel
// inline form) render this identically through `HitlForm`.
export function goalFormPayload(): HitlPayload {
  return {
    kind: "form",
    title: "New goal",
    description: "A testable outcome the agent self-drives toward. The verifier decides DONE.",
    steps: [
      {
        title: "Goal",
        schema: {
          type: "object",
          required: ["condition"],
          properties: {
            condition: {
              type: "string",
              title: "Goal",
              description: "The outcome the agent should achieve — in plain English.",
            },
            verifier: {
              type: "string",
              title: "How to verify it's done",
              default: "llm",
              oneOf: GOAL_VERIFIER_TYPES.map((v) => ({
                const: v.value,
                title: v.label,
                description: v.description,
              })),
            },
            verify_command: {
              type: "string",
              title: "Shell command",
              description: "Runs on the server; exit 0 = done (e.g. `pytest -q`).",
              showWhen: { field: "verifier", in: ["command", "test"] },
            },
            verify_ci: {
              type: "string",
              title: "PR # or branch",
              description: "GitHub checks must be green (e.g. `#1785` or `main`).",
              showWhen: { field: "verifier", equals: "ci" },
            },
            verify_data_path: {
              type: "string",
              title: "File to check",
              description: "Path to a file the goal writes or updates.",
              showWhen: { field: "verifier", equals: "data" },
            },
            verify_data_contains: {
              type: "string",
              title: "Must contain (optional)",
              description: "Done when the file contains this substring — blank just requires the file.",
              showWhen: { field: "verifier", equals: "data" },
            },
          },
        },
      },
      {
        title: "Completion contract (optional)",
        description: "Extra guidance the agent re-reads each turn. Everything here is optional.",
        schema: {
          type: "object",
          properties: {
            outcome: {
              type: "string",
              title: "Outcome",
              description: "The required end-state, in one line. Defaults to the goal.",
            },
            constraints: {
              type: "string",
              format: "textarea",
              title: "Constraints",
              description: "Invariants the agent must NOT violate — one per line.",
            },
            boundaries: {
              type: "string",
              format: "textarea",
              title: "Boundaries",
              description: "Files / dirs / systems in scope — one per line.",
            },
            stop_when: {
              type: "string",
              title: "Stop and ask when",
              description: "A condition under which the agent pauses and asks you.",
            },
            max_iterations: {
              type: "number",
              title: "Max iterations",
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

/** The verifier's free-text detail, read from the TYPE-AWARE field(s) for the picked verifier
 *  and normalized to the `path :: substring` form `buildVerifier` parses for `data`. Empty for
 *  llm (and any verifier whose detail field is blank). */
export function verifierDetail(answers: Record<string, unknown>): string {
  const t = String(answers.verifier ?? "").trim().toLowerCase();
  if (t === "command" || t === "test") return String(answers.verify_command ?? "").trim();
  if (t === "ci") return String(answers.verify_ci ?? "").trim();
  if (t === "data") {
    const path = String(answers.verify_data_path ?? "").trim();
    const contains = String(answers.verify_data_contains ?? "").trim();
    return contains ? `${path} :: ${contains}` : path;
  }
  return "";
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

  const verifier = buildVerifier(answers.verifier, verifierDetail(answers));
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
