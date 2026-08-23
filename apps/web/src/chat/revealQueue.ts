// Client-side reveal queue — smooths chunky provider streaming (#2993).
//
// Diagnosis: Claude OAuth models (the anthropic-oauth provider) rendered answer
// text in visible ~8-9 word blocks even though the server's executor flush
// logic is correct (_FLUSH_CHARS=24 / _FLUSH_INTERVAL_S=0.1 in
// a2a_impl/executor.py, guarded by tests/test_a2a_flush_granularity.py). The
// chunking is UPSTREAM of the executor: the Anthropic SDK (via
// langchain-anthropic) yields AIMessageChunks in multi-word bursts — whole
// clauses per chunk — so by the time the server sees the text there is nothing
// finer left to flush, and each burst reaches the console as one SSE
// artifact-update frame. LiteLLM gateway models yield ~per-token chunks, which
// is why they always looked smooth. (The [stream-delta] DEBUG log in
// server/chat.py's on_chat_model_stream branch is the delta-profile
// measurement behind this.)
//
// The fix is presentation-side and uniform across providers: instead of
// rendering each frame the instant it arrives, streamed deltas are buffered as
// word chunks and dripped out at a steady cadence (~a word every
// REVEAL_MS_PER_WORD ms) on a requestAnimationFrame loop. flush() reveals
// everything synchronously — callers invoke it whenever ordering or finality
// matters (a tool / reasoning / component frame that must interleave with the
// text at its true position, the terminal REPLACE frame, stream
// end/error/Stop) — so the final answer is never delayed and part ordering is
// never reshuffled by the pacing.

import { splitRevealChunks } from "./parts";

/** Reveal cadence — one word chunk per this many ms (the ~30-60ms/word band). */
export const REVEAL_MS_PER_WORD = 35;

/** Backlog ceiling, in word chunks. A fast model can outrun the drip; any tick
 *  reveals enough extra to keep at most this many chunks queued, bounding how
 *  far the rendered text can lag the wire (~maxBacklog × msPerWord ≈ 2s). */
export const REVEAL_MAX_BACKLOG_CHUNKS = 60;

export interface RevealQueueOptions {
  /** Land a run of revealed text (the caller appends it to the message). */
  apply: (text: string) => void;
  msPerWord?: number;
  maxBacklog?: number;
  /** Injectable frame scheduler / clock (tests). Default: rAF + performance.now. */
  raf?: (fn: () => void) => number;
  caf?: (handle: number) => void;
  now?: () => number;
}

export interface RevealQueue {
  /** Enqueue a streamed delta for paced reveal. */
  push: (text: string) => void;
  /** Reveal everything still queued NOW (synchronously) and cancel the frame
   *  loop. Idempotent; a later push starts a fresh paced run. */
  flush: () => void;
  /** Word chunks still withheld (tests / diagnostics). */
  pending: () => number;
}

export function createRevealQueue(opts: RevealQueueOptions): RevealQueue {
  const msPerWord = opts.msPerWord ?? REVEAL_MS_PER_WORD;
  const maxBacklog = opts.maxBacklog ?? REVEAL_MAX_BACKLOG_CHUNKS;
  const raf = opts.raf ?? ((fn: () => void) => window.requestAnimationFrame(fn));
  const caf = opts.caf ?? ((handle: number) => window.cancelAnimationFrame(handle));
  const now = opts.now ?? (() => performance.now());

  let queue: string[] = [];
  let handle: number | undefined;
  // Time-budget pacing: each tick banks the elapsed ms and spends msPerWord per
  // chunk, carrying the remainder — so the cadence holds at any frame rate (60fps
  // ticks don't reveal a word each, and a slow/throttled tab catches up in one
  // burst instead of falling permanently behind).
  let lastTick = 0;
  let budget = 0;

  const schedule = () => {
    if (handle === undefined) handle = raf(tick);
  };

  // Hoisted declaration (same idiom as streamWatchdog's onIdle) — tick and
  // schedule reference each other.
  function tick() {
    handle = undefined;
    if (!queue.length) return;
    const t = now();
    budget += t - lastTick;
    lastTick = t;
    let n = Math.floor(budget / msPerWord);
    budget -= n * msPerWord;
    // Backlog ceiling: reveal extra (for free) rather than let the render lag grow.
    if (queue.length - n > maxBacklog) n = queue.length - maxBacklog;
    if (n > 0) {
      opts.apply(queue.slice(0, n).join(""));
      queue = queue.slice(n);
    }
    if (queue.length) schedule();
  }

  return {
    push(text: string) {
      if (!text) return;
      const chunks = splitRevealChunks(text);
      if (!chunks.length) return;
      if (!queue.length) {
        // Fresh run: reset the clock (idle time must not convert into a burst)
        // and pre-fund one chunk so the first word shows on the next frame.
        lastTick = now();
        budget = msPerWord;
      }
      queue.push(...chunks);
      schedule();
    },
    flush() {
      if (handle !== undefined) {
        caf(handle);
        handle = undefined;
      }
      if (!queue.length) return;
      const rest = queue.join("");
      queue = [];
      budget = 0;
      opts.apply(rest);
    },
    pending: () => queue.length,
  };
}
