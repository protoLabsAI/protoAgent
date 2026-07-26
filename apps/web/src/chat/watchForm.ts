// Pure logic for the watch-creation form (ADR 0067) — the HitlPayload the Watches panel and
// the Work overview's Watches card both render through the SAME `HitlForm` as the goal
// creator, plus the answers→`POST /api/watches` body mapping. Kept out of the React/DS layer
// (no `@protolabsai/ui` imports, only a type-only `HitlPayload`) so the web unit suite can
// exercise the schema shape + the verifier/cadence mapping directly — mirroring `goalForm.ts`,
// which this deliberately parallels field-for-field wherever the two primitives agree.
//
// Why an operator form exists at all: a watch used to be agent-created only, so the one thing
// an operator could do from the console was CLEAR one. But the operator channel is the *more*
// capable of the two — `POST /api/watches` is trusted (ADR 0066 path ceiling), so it accepts
// shell/test/ci/data verifiers the agent is denied. The console was hiding the stronger half.

import type { HitlPayload, VerifierCatalog } from "../lib/types";

// Fallback when the catalog hasn't loaded (or `GET /api/verifiers` failed): the core types,
// minus `plugin`, which is meaningless without knowing what's registered. The REAL list comes
// from the server — a hardcoded UI copy of a server registry is exactly how this field lost
// the entire plugin class in the first place.
export const FALLBACK_VERIFIER_TYPES: VerifierCatalog = {
  types: [
    { value: "command", description: "A shell command that exits 0", source: "core" },
    { value: "test", description: "A test command that exits 0", source: "core" },
    { value: "ci", description: "GitHub checks are green (PR # or branch)", source: "core" },
    { value: "data", description: "Assert over a file's contents", source: "core" },
    { value: "llm", description: "Fuzzy LLM judgment (the default)", source: "core" },
  ],
  plugin_checks: [],
};

// The session a console-created watch reacts into. Matches the goal creator's `"operator"`
// target, so a trip lands in the same place an operator-set goal does.
export const OPERATOR_SESSION = "operator";

// `30m` / `2h` / `7d` — the same duration vocabulary `graph/sdk.py` already uses for
// schedules, so one spelling covers cadences, expiries and cron sugar across the product.
const DURATION_RE = /^\s*(\d+)\s*([smhd])\s*$/i;
const UNIT_SECONDS: Record<string, number> = { s: 1, m: 60, h: 3600, d: 86400 };

/** `"2h"` → 7200. Returns null for blank or unparseable input — the caller omits the field
 *  rather than sending a guess, so a typo can't silently arm a 0-second cadence. */
export function parseDuration(raw: unknown): number | null {
  if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) return raw;
  if (typeof raw !== "string") return null;
  const m = DURATION_RE.exec(raw);
  if (!m) return null;
  const n = Number(m[1]);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n * UNIT_SECONDS[m[2].toLowerCase()];
}

// Two steps, mirroring the goal wizard. Step 1 = what to watch and how to ground it, with the
// SAME type-aware conditional inputs (only the fields the picked verifier needs). Step 2 = the
// watch-specific half a goal has no analogue for: how hard to poll, when to give up, and what
// to DO when it trips — the last being the whole point of the primitive.
export function watchFormPayload(catalog: VerifierCatalog = FALLBACK_VERIFIER_TYPES): HitlPayload {
  const checks = catalog.plugin_checks ?? [];
  // Offer `plugin` only when something registers a check — a card whose picker would be
  // empty is worse than no card. Everything else comes straight from the server's list.
  const types = (catalog.types ?? []).filter((v) => v.value !== "plugin" || checks.length > 0);
  return {
    kind: "form",
    title: "New watch",
    description:
      "A condition something else moves, polled out-of-band. When it trips, the agent can react.",
    steps: [
      {
        title: "Watch",
        schema: {
          type: "object",
          required: ["condition"],
          properties: {
            condition: {
              type: "string",
              title: "Watch for",
              description: "What you're waiting on — in plain English (e.g. `the deploy finishes`).",
            },
            verifier: {
              type: "string",
              title: "How to check it",
              default: "llm",
              oneOf: types.map((v) => ({
                const: v.value,
                title: v.value,
                description: v.description,
              })),
            },
            verify_plugin_check: {
              type: "string",
              title: "Plugin check",
              description: "A check contributed by an installed plugin — it reads real state, not the transcript.",
              // Namespaced `<plugin-id>:<check>` by the registry, so the id is unambiguous;
              // the plugin's own description rides alongside when it registered one.
              oneOf: checks.map((c) => ({
                const: c.name,
                title: c.name,
                description: c.description || `from ${c.plugin_id}`,
              })),
              showWhen: { field: "verifier", equals: "plugin" },
            },
            verify_plugin_args: {
              type: "string",
              title: "Check arguments (optional)",
              description:
                'JSON the check reads, e.g. `{"min": 1000000}`. Each plugin documents its own; invalid JSON is dropped rather than sent.',
              showWhen: { field: "verifier", equals: "plugin" },
            },
            verify_command: {
              type: "string",
              title: "Shell command",
              description: "Runs on the server each poll; exit 0 = tripped (e.g. `kubectl rollout status deploy/api`).",
              showWhen: { field: "verifier", in: ["command", "test"] },
            },
            verify_ci: {
              type: "string",
              title: "PR # or branch",
              description: "Tripped when GitHub checks are green (e.g. `#1785` or `main`).",
              showWhen: { field: "verifier", equals: "ci" },
            },
            verify_data_path: {
              type: "string",
              title: "File to check",
              description: "Path to a file whatever you're watching writes or updates.",
              showWhen: { field: "verifier", equals: "data" },
            },
            verify_data_contains: {
              type: "string",
              title: "Must contain (optional)",
              description: "Tripped when the file contains this substring — blank just requires the file.",
              showWhen: { field: "verifier", equals: "data" },
            },
          },
        },
      },
      {
        title: "Cadence & reaction (optional)",
        description: "How hard to poll, when to give up, and what to do when it trips.",
        schema: {
          type: "object",
          properties: {
            mode: {
              type: "string",
              title: "Fire",
              default: "once",
              // The primitive's two axes collapsed into one operator-facing choice, because
              // trigger×repeat has four combinations and only three are meaningful.
              oneOf: [
                { const: "once", title: "once, then stop", description: "A tripwire — the original behaviour" },
                {
                  const: "repeat",
                  title: "every time it becomes true",
                  description: "Fires on each rising edge, not once per check while it stays true",
                },
                {
                  const: "change",
                  title: "whenever the value changes",
                  description: "A monitor — reports movement, whatever the condition says",
                },
              ],
            },
            run_prompt: {
              type: "string",
              format: "textarea",
              title: "When it trips, tell the agent to…",
              description:
                "Runs as a follow-up turn with the watch's evidence attached. Leave blank and the watch just records the trip.",
            },
            interval: {
              type: "string",
              title: "Check every",
              description: "`30m`, `2h`, `1d`. Blank uses the instance cadence. Only ever slower, never faster.",
            },
            expires_in: {
              type: "string",
              title: "Give up after",
              description:
                "`6h`, `7d`. Blank watches indefinitely; past it the watch finishes `expired`. Worth setting for a repeating watch — nothing else ends one.",
            },
            stall_after: {
              type: "number",
              title: "Flag a stall after",
              description: "N consecutive checks with unchanged evidence — how you notice something wedged, not slow.",
            },
          },
        },
      },
    ],
  };
}

/** The `POST /api/watches` body (operator watch-create, ADR 0067 D4 — trusted, any verifier). */
export type WatchCreateBody = {
  condition: string;
  verifier: Record<string, unknown>;
  trigger?: string;
  repeat?: boolean;
  interval_s?: number;
  deadline?: number;
  stall_after?: number;
  run_prompt?: string;
  run_session?: string;
};

const str = (v: unknown): string => (typeof v === "string" ? v.trim() : "");

/** Map the picked verifier + its one conditional input into the opaque verifier dict the
 *  backend validates. Mirrors `buildGoalSetBody`'s verifier half so the two primitives can't
 *  drift into different spellings of the same spec. */
function buildVerifier(answers: Record<string, unknown>): Record<string, unknown> {
  const type = str(answers.verifier) || "llm";
  switch (type) {
    case "command":
      return { type: "command", command: str(answers.verify_command) };
    case "test":
      return { type: "test", command: str(answers.verify_command) };
    case "ci": {
      const ref = str(answers.verify_ci).replace(/^#/, "");
      // A bare number is a PR; anything else is a branch — same split the goal form uses.
      return /^\d+$/.test(ref) ? { type: "ci", pr: ref } : { type: "ci", branch: ref };
    }
    case "plugin": {
      const spec: Record<string, unknown> = { type: "plugin", check: str(answers.verify_plugin_check) };
      // `args` is declarative data the plugin's own verifier validates — we can't know its
      // shape, so pass it through when it parses and omit it when it doesn't. The verifier
      // reports what it actually needed, which beats us guessing a schema we don't have.
      const raw = str(answers.verify_plugin_args);
      if (raw) {
        try {
          const parsed: unknown = JSON.parse(raw);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) spec.args = parsed;
        } catch {
          /* not JSON — omit rather than send a string the verifier can't read */
        }
      }
      return spec;
    }
    case "data": {
      const spec: Record<string, unknown> = { type: "data", path: str(answers.verify_data_path) };
      const contains = str(answers.verify_data_contains);
      if (contains) spec.contains = contains;
      return spec;
    }
    default:
      return { type: "llm" };
  }
}

/** Answers → the create body, or `null` when there's no condition (the one required field).
 *
 *  `now` is injected so the deadline mapping is testable: the form asks for a DURATION
 *  ("give up after 6h") because that's what an operator actually knows, while the API stores
 *  an absolute epoch — the same relative-in/absolute-stored split the `create_watch` tool
 *  uses, and for the same reason: a wall-clock timestamp is the awkward half to author. */
export function buildWatchCreateBody(
  answers: Record<string, unknown>,
  now: number = Date.now(),
): WatchCreateBody | null {
  const condition = str(answers.condition);
  if (!condition) return null;

  const body: WatchCreateBody = { condition, verifier: buildVerifier(answers) };

  const interval = parseDuration(answers.interval);
  if (interval) body.interval_s = interval;

  const expires = parseDuration(answers.expires_in);
  if (expires) body.deadline = Math.round(now / 1000) + expires;

  // "once" is the default and sends neither field, so a body from this form stays
  // byte-identical to the pre-monitor shape unless the operator opted in.
  const mode = str(answers.mode) || "once";
  if (mode === "change") {
    body.trigger = "change";
    body.repeat = true;
  } else if (mode === "repeat") {
    body.repeat = true;
  }

  const stall = Number(answers.stall_after);
  if (Number.isFinite(stall) && stall > 0) body.stall_after = Math.round(stall);

  const prompt = str(answers.run_prompt);
  if (prompt) {
    body.run_prompt = prompt;
    // Only meaningful paired with a session — a reaction with nowhere to run is dropped by
    // the controller, so send them together or not at all.
    body.run_session = OPERATOR_SESSION;
  }
  return body;
}
