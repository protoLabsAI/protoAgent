import type { Delegation } from "../lib/types";
import type { JobStatus } from "./backgroundJobStore";

// The delegation row's pure logic — what it says and which state it shows — kept apart from
// the component so it's unit-testable without a DOM.

/** The row's state:
 *  - `running` / `done` / `failed` — a background delegation, tracked by its job;
 *  - `failed` — also any dispatch that errored before it started;
 *  - `sent`   — a foreground delegation (its reply follows as the delegate's own message) or a
 *    background one whose job isn't known yet (no spinner: an unknown job must not spin forever). */
export type DelegationState = "running" | "done" | "failed" | "sent";

export function delegationState(d: Delegation | undefined, job: JobStatus | undefined): DelegationState {
  if (d?.error) return "failed";
  if (!d?.background) return "sent";
  if (job === "running") return "running";
  if (job === "completed") return "done";
  if (job === "failed" || job === "canceled") return "failed";
  return "sent";
}

/** The prompt's opening sentence (or line), whitespace-flattened and clipped — the row's
 *  summary when the ask carries none (a server from before `summary`, or a history row). */
export function briefSummary(text: string, max = 120): string {
  const first = (text.trim().split(/(?<=[.!?])\s|\n/)[0] ?? "").replace(/\s+/g, " ").trim();
  return first.length > max ? `${first.slice(0, max - 1).trimEnd()}…` : first;
}

export const STATE_LABEL: Record<DelegationState, string> = {
  running: "running in the background",
  done: "finished",
  failed: "failed",
  sent: "sent",
};
