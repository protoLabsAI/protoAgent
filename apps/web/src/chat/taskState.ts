// One classification of an A2A task state for every console surface that settles a turn
// off one (#3957). The surfaces each carried their own regexes, and they had drifted: the
// TERMINAL set included `rejected` while the failure check was `/fail|cancel/`, so a turn
// the agent REJECTED was settled as a clean "done".
//
// Matches both wire spellings: A2A 0.3 (`completed`, `input-required`, `unknown`) and
// A2A 1.0 (`TASK_STATE_COMPLETED`, `TASK_STATE_INPUT_REQUIRED`, `TASK_STATE_UNSPECIFIED`).
//
//   terminal  — the turn is over: completed, failed, canceled, rejected.
//   failed    — the terminal states that did not succeed: failed, canceled, rejected.
//   paused    — parked on the operator (input-required / auth-required): not over, the
//               operator's answer continues the same task, and not working either.
//   unknown   — the server cannot say (0.3 `unknown`, 1.0 `TASK_STATE_UNSPECIFIED`). No
//               producer will ever move it on, so a reader settles it the way it settles a
//               task that is gone, rather than waiting on it.

const TERMINAL = /completed|failed|canceled|cancelled|rejected/i;
const FAILED = /failed|canceled|cancelled|rejected/i;
const PAUSED = /input.required|auth.required/i;
const UNKNOWN = /^(?:task_state_)?(?:unknown|unspecified)$/i;

export function isTaskTerminal(state: string | undefined): boolean {
  return TERMINAL.test(state ?? "");
}

export function isTaskFailed(state: string | undefined): boolean {
  return FAILED.test(state ?? "");
}

export function isTaskPaused(state: string | undefined): boolean {
  return PAUSED.test(state ?? "");
}

export function isTaskStateUnknown(state: string | undefined): boolean {
  return UNKNOWN.test((state ?? "").trim());
}
