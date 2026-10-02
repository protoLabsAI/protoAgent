import type { QueryClient } from "@tanstack/react-query";

import { tidyPath } from "./store";

// Live updates for the code pane (ADR 0112). The Diff tab and the open file used to be
// fetched once and then sit still — a coding delegate could rewrite half the project while
// the pane said "No changes vs HEAD" until the operator clicked Refresh. Two signals now
// re-fetch them:
//
//  1. `fs.changed` on the event bus — the server publishes it when a write it OBSERVES lands
//     inside a registered project: a coding delegate's settled edit tool call, or the agent's
//     own write_file / edit_file / delete_file (graph/fs_changes.py). CodeChangeWatch feeds it
//     here.
//  2. A cheap stamp poll (`/api/fs/stamp`, every ~2 s, only while the pane is on screen and
//     the tab is visible) for edits nothing reports — a terminal, an editor, a coder's shell.
//
// Both land in the query cache the same way (`invalidateCodePane`); the bus path is
// debounced and coalesced first, since a coder can settle a dozen edits in a second.

export const FS_CHANGED_TOPIC = "fs.changed";
/** Quiet period before a burst of changes is applied. */
export const REFRESH_DEBOUNCE_MS = 300;
/** …but a steady stream still refreshes at least this often. */
export const REFRESH_MAX_WAIT_MS = 1_000;
/** The fallback poll's cadence while the pane is on screen. */
export const STAMP_POLL_MS = 2_000;
/** After a failed poll (timeout, git error), wait this long before asking again. */
export const STAMP_BACKOFF_MS = 10_000;

export type FsChange = {
  project: string;
  /** Project-relative paths; empty = "something in the project" (refetch everything). */
  paths: string[];
  /** `agent` (protoAgent's own fs tools) or `delegate` (a coder's tool call). */
  source: string;
  target?: string;
};

/** A bus payload → an FsChange, or null when it isn't one. */
export function parseFsChanged(data: Record<string, unknown> | null | undefined): FsChange | null {
  const project = typeof data?.project === "string" ? data.project.trim() : "";
  if (!project) return null;
  const raw = Array.isArray(data?.paths) ? data.paths : [];
  const paths = raw.filter((p): p is string => typeof p === "string" && p.trim() !== "").map(tidyPath);
  const source = typeof data?.source === "string" ? data.source : "";
  const target = typeof data?.target === "string" && data.target ? data.target : undefined;
  return { project, paths, source, ...(target ? { target } : {}) };
}

/** What changed, per project: a set of paths, or `"all"` when any change named none. */
export type ChangeBatch = Map<string, Set<string> | "all">;

/** Debounce + coalesce: `note()` any number of changes; `onFlush` gets ONE batch per burst,
 *  REFRESH_DEBOUNCE_MS after the last note — or REFRESH_MAX_WAIT_MS after the first, so a
 *  coder editing non-stop still shows its work. */
export function createChangeCoalescer(
  onFlush: (batch: ChangeBatch) => void,
  { debounceMs = REFRESH_DEBOUNCE_MS, maxWaitMs = REFRESH_MAX_WAIT_MS }: { debounceMs?: number; maxWaitMs?: number } = {},
) {
  let batch: ChangeBatch = new Map();
  let timer: ReturnType<typeof setTimeout> | null = null;
  let firstAt = 0;

  const flush = () => {
    if (timer) clearTimeout(timer);
    timer = null;
    if (batch.size === 0) return;
    const out = batch;
    batch = new Map();
    onFlush(out);
  };

  return {
    note(project: string, paths: string[] = []): void {
      if (!project) return;
      const prev = batch.get(project);
      if (paths.length === 0 || prev === "all") batch.set(project, "all");
      else batch.set(project, new Set([...(prev ?? []), ...paths]));
      const now = Date.now();
      if (!timer) firstAt = now;
      if (timer) clearTimeout(timer);
      const wait = Math.max(0, Math.min(debounceMs, firstAt + maxWaitMs - now));
      timer = setTimeout(flush, wait);
    },
    flush,
    dispose(): void {
      if (timer) clearTimeout(timer);
      timer = null;
      batch = new Map();
    },
  };
}

/** Query keys the pane reads (CodePane.tsx) — kept here so the invalidation can't drift. */
export const diffQueryKey = (project: string) => ["code-pane-diff", project] as const;
export const fileQueryPrefix = (project: string, path?: string) =>
  (path ? ["code-pane-file", project, path] : ["code-pane-file", project]) as readonly unknown[];

/** Re-fetch what a batch touched: each project's diff, and its open file(s) — every cached
 *  file of the project for an `"all"`, else just the named paths. Only ACTIVE queries refetch
 *  now; an inactive one is marked stale for its next mount. */
export function invalidateCodePane(qc: QueryClient, batch: ChangeBatch): void {
  for (const [project, paths] of batch) {
    void qc.invalidateQueries({ queryKey: diffQueryKey(project) });
    if (paths === "all") void qc.invalidateQueries({ queryKey: fileQueryPrefix(project) });
    else for (const p of paths) void qc.invalidateQueries({ queryKey: fileQueryPrefix(project, p) });
  }
}
