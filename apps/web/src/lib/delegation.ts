import type { Delegation } from "./types";

/** The delegation row's fields off an outgoing-ask frame; undefined when it carries none
 *  (an older server) — the row then falls back to the prompt's first sentence. */
export function delegationFromFrame(d: {
  summary?: unknown;
  background?: unknown;
  job_id?: unknown;
  error?: unknown;
  id?: unknown;
}): Delegation | undefined {
  const out: Delegation = {};
  // The ask's own id keys its live progress (#3975); a foreground delegation has one.
  if (typeof d.id === "string" && d.id) out.id = d.id;
  if (typeof d.summary === "string" && d.summary.trim()) out.summary = d.summary.trim();
  if (d.background === true) out.background = true;
  if (typeof d.job_id === "string" && d.job_id) out.jobId = d.job_id;
  if (typeof d.error === "string" && d.error) out.error = d.error;
  return Object.keys(out).length ? out : undefined;
}
