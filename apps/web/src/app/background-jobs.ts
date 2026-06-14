// Pure helpers for the background-jobs UtilityBar widget (ADR 0050 Phase 3).
// Kept React-free so they're unit-testable without a DOM/react-dom import.

import type { BackgroundJobDTO } from "../lib/types";

export function nowIso(): string {
  return new Date().toISOString();
}

export function fmtElapsed(startIso?: string, endIso?: string): string {
  if (!startIso) return "";
  const start = Date.parse(startIso);
  const end = endIso ? Date.parse(endIso) : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end)) return "";
  let s = Math.max(0, Math.round((end - start) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  s = s % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// A background turn's tool, as surfaced by the `background.progress` channel (ADR 0051).
export type ProgressTool = { id: string; tool: string; done: boolean; error: boolean };

/** Merge a `background.progress` frame into a job's tool list, keyed by tool_call_id
 *  (tool_start → running, tool_end → done/error). Capped so a chatty job can't grow
 *  unbounded. Pure → unit-testable. */
export function applyProgress(
  prev: ProgressTool[],
  frame: { phase?: string; tool?: string; tool_call_id?: string; error?: boolean },
): ProgressTool[] {
  const id = String(frame.tool_call_id || frame.tool || "");
  if (!id) return prev;
  const next = prev.slice();
  const i = next.findIndex((t) => t.id === id);
  const entry: ProgressTool = {
    id,
    tool: String(frame.tool || (i >= 0 ? next[i].tool : "tool")),
    done: frame.phase === "tool_end",
    error: !!frame.error,
  };
  if (i >= 0) next[i] = entry;
  else next.push(entry);
  return next.slice(-8);
}

/** Sort order for the jobs list: running first, then most-recently-touched. */
export function byRecency(a: BackgroundJobDTO, b: BackgroundJobDTO): number {
  if (a.status === "running" && b.status !== "running") return -1;
  if (b.status === "running" && a.status !== "running") return 1;
  const at = Date.parse(a.completed_at || a.created_at || "") || 0;
  const bt = Date.parse(b.completed_at || b.created_at || "") || 0;
  return bt - at;
}
