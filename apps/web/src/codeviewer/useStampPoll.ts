import { useEffect, useRef } from "react";

import { api, ApiError } from "../lib/api";
import { STAMP_BACKOFF_MS, STAMP_POLL_MS, STAMP_SLOW_MAX_MS } from "./liveRefresh";

/** The code pane's fallback change signal (ADR 0112): while the pane is mounted, ask
 *  `/api/fs/stamp` for `project` (and the open `path`) every STAMP_POLL_MS and call
 *  `onChange` when the fingerprint moves — catching edits no tool reported (a terminal, an
 *  editor, a coder's shell command). The bus (`fs.changed`) covers tracked writes with lower
 *  latency; this is the net under it.
 *
 *  Why a client poll of a server fingerprint, not a server-side watcher: it costs nothing
 *  unless a pane is actually on screen (no watcher threads, no subscription bookkeeping, no
 *  new dependency — neither watchfiles nor watchdog is in the lock), it stops by itself when
 *  the pane unmounts, and one hardened `git status` + lstats is cheap next to the full diff
 *  it saves re-fetching. Skipped while the browser tab is hidden; a 404 (an older server, or
 *  the toolset switched off) stops it; other failures back off. */
/** The poll's next delay: reset on a change or a fast answer, doubled (capped) on a slow one. */
export function nextStampInterval(current: number, slow: boolean, moved: boolean): number {
  if (moved || !slow) return STAMP_POLL_MS;
  return Math.min(STAMP_SLOW_MAX_MS, Math.max(STAMP_POLL_MS, current * 2));
}

export function useStampPoll(project: string | null, path: string | null, onChange: () => void): void {
  const cb = useRef(onChange);
  cb.current = onChange;
  useEffect(() => {
    if (!project) return;
    let last: string | null = null;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    // The current cadence: STAMP_POLL_MS, doubled (up to STAMP_SLOW_MAX_MS) for every answer
    // the server marks `slow` — a huge repo's stamp must not cost a core per open pane — and
    // back to STAMP_POLL_MS the moment the stamp moves or a stamp comes back fast.
    let interval = STAMP_POLL_MS;
    const schedule = (ms: number) => {
      if (!stopped) timer = setTimeout(() => void tick(), ms);
    };
    const tick = async () => {
      if (stopped) return;
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return schedule(interval);
      try {
        const r = await api.fsStamp(project, path ?? undefined);
        if (stopped) return;
        const moved = last !== null && r.stamp !== last;
        if (moved) cb.current();
        last = r.stamp;
        interval = nextStampInterval(interval, Boolean(r.slow), moved);
        schedule(interval);
      } catch (e) {
        if (stopped || (e instanceof ApiError && e.status === 404)) return;
        schedule(STAMP_BACKOFF_MS);
      }
    };
    void tick(); // the baseline
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [project, path]);
}
