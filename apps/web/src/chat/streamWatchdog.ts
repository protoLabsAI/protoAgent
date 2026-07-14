// Stalled-stream watchdog (hung-workblock fix).
//
// The chat's only "turn done" signal is the A2A SSE stream closing (onDone) —
// there is no standalone terminal event on the wire. If the stream stalls open
// mid-turn — e.g. a large answer whose terminal frames were stranded when the
// server's producer got cancelled on teardown (a2a_impl/registry.py grants a
// 0.5s grace then cancels), or a proxy/tailnet buffer — the reader blocks
// forever, so onDone, the post-stream reconcile, and the turn's `finally` never
// run and the bubble spins "Working…" until reload.
//
// This watchdog turns "stuck forever" into "self-heals in seconds": reset it on
// every stream frame; after an idle window with no frames, consult the durable
// task (A2A tasks/get). If the task is TERMINAL, the server finished and the
// stream tail was lost — fire `onTerminal` so the caller finalizes the bubble
// from the authoritative task. If it's still working, the turn is just
// legitimately quiet (a slow tool) — keep waiting. It never fabricates a
// completion the server didn't record.

export type WatchdogTaskState = { state: string; text: string };

const TERMINAL_RE = /completed|failed|canceled|cancelled/i;

/**
 * True when a task should be treated as settled. A missing/empty state means the
 * task is gone from the store — un-stick the turn rather than spin forever.
 */
export function isTerminalTaskState(state: string): boolean {
  return !state || TERMINAL_RE.test(state);
}

export interface StreamWatchdogOptions {
  /** Idle window (ms) with no stream frames before we consult the durable task. */
  idleMs: number;
  /**
   * Fetch authoritative task state (A2A tasks/get). Reject to signal
   * "unknown — retry later" (task id not surfaced yet, or the server is briefly
   * unreachable); the watchdog re-arms instead of giving up.
   */
  getTask: () => Promise<WatchdogTaskState>;
  /** Invoked at most once, when the task is terminal but the stream never delivered it. */
  onTerminal: (task: WatchdogTaskState) => void;
  /** Injectable timer (tests). Defaults to the global timers. */
  setTimer?: (fn: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimer?: (handle: ReturnType<typeof setTimeout>) => void;
}

export interface StreamWatchdog {
  /** Reset the idle countdown — call on every stream frame that proves liveness. */
  bump: () => void;
  /** Stop for good (the stream settled the turn itself, errored, or unmounted). */
  stop: () => void;
  /** Whether the watchdog has already fired `onTerminal` or been stopped. */
  settled: () => boolean;
}

export function createStreamWatchdog(opts: StreamWatchdogOptions): StreamWatchdog {
  const setTimer = opts.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
  const clearTimer = opts.clearTimer ?? ((h) => clearTimeout(h));
  let handle: ReturnType<typeof setTimeout> | undefined;
  let settled = false;

  const clear = () => {
    if (handle !== undefined) {
      clearTimer(handle);
      handle = undefined;
    }
  };

  const bump = () => {
    if (settled) return;
    clear();
    handle = setTimer(onIdle, opts.idleMs);
  };

  const stop = () => {
    settled = true;
    clear();
  };

  async function onIdle() {
    if (settled) return;
    let task: WatchdogTaskState;
    try {
      task = await opts.getTask();
    } catch {
      bump(); // task id not ready / server unreachable — retry, don't give up
      return;
    }
    if (settled) return;
    if (!isTerminalTaskState(task.state)) {
      bump(); // genuinely still working (a long, quiet tool) — keep waiting
      return;
    }
    settled = true;
    clear();
    opts.onTerminal(task);
  }

  return { bump, stop, settled: () => settled };
}
