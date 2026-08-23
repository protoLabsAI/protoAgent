import { describe, expect, it } from "vitest";

import { createRevealQueue, REVEAL_MS_PER_WORD } from "./revealQueue";

// Deterministic harness: injectable clock + frame scheduler (same idiom as
// streamWatchdog.test.ts). `frame(ms)` advances the clock and fires the one
// scheduled rAF callback, mimicking a browser frame that arrives `ms` later.
function harness(opts: { msPerWord?: number; maxBacklog?: number } = {}) {
  let t = 0;
  let scheduled: (() => void) | null = null;
  const applied: string[] = [];
  const queue = createRevealQueue({
    apply: (text) => applied.push(text),
    raf: (fn) => {
      scheduled = fn;
      return 1;
    },
    caf: () => {
      scheduled = null;
    },
    now: () => t,
    ...opts,
  });
  const frame = (advanceMs: number) => {
    t += advanceMs;
    const fn = scheduled;
    scheduled = null;
    fn?.();
  };
  return {
    queue,
    frame,
    applied,
    out: () => applied.join(""),
    hasFrame: () => scheduled !== null,
    idle: (ms: number) => {
      t += ms; // wall-clock passes with NO frame pending (queue drained)
    },
  };
}

describe("createRevealQueue", () => {
  it("drips a multi-word burst out ~one word per cadence interval, not all at once", () => {
    // The Claude OAuth failure mode: one SSE frame carrying an 8-word block.
    const h = harness({ msPerWord: 35 });
    h.queue.push("one two three four five six seven eight");
    // First frame (~16ms later): the pre-funded first chunk only — not the block.
    h.frame(16);
    expect(h.out()).toBe("one");
    expect(h.queue.pending()).toBe(7);
    // 60fps frames: cadence holds at ~35ms/word — two more frames buy ONE word
    // (16+16=32ms < 2×35), never one word per frame.
    h.frame(16);
    h.frame(16);
    expect(h.out()).toBe("one two");
    // Walk the rest out at the cadence.
    h.frame(35);
    expect(h.out()).toBe("one two three");
    h.frame(35);
    h.frame(35);
    h.frame(35);
    h.frame(35);
    h.frame(35);
    expect(h.out()).toBe("one two three four five six seven eight");
    expect(h.queue.pending()).toBe(0);
    expect(h.hasFrame()).toBe(false); // drained — the frame loop stops
  });

  it("catches up after a slow/throttled frame instead of falling behind", () => {
    const h = harness({ msPerWord: 35 });
    h.queue.push("a b c d e f");
    // One late frame (a busy main thread / background tab): banks the elapsed
    // time and reveals the words it covers in one burst.
    h.frame(35 * 4);
    expect(h.out()).toBe("a b c d e"); // 4 elapsed intervals + the pre-funded first chunk
    h.frame(35);
    expect(h.out()).toBe("a b c d e f");
  });

  it("bounds the backlog so a fast stream can't lag the render arbitrarily", () => {
    const h = harness({ msPerWord: 35, maxBacklog: 3 });
    h.queue.push("w1 w2 w3 w4 w5 w6 w7 w8 w9 w10");
    h.frame(16); // cadence alone would reveal 1 chunk, leaving 9 queued
    expect(h.queue.pending()).toBe(3); // …but the ceiling force-drains to maxBacklog
    expect(h.out()).toBe("w1 w2 w3 w4 w5 w6 w7");
  });

  it("flush reveals everything queued synchronously and cancels the frame loop", () => {
    const h = harness();
    h.queue.push("the final answer never waits");
    h.frame(16);
    expect(h.out()).toBe("the");
    h.queue.flush(); // the terminal REPLACE / done path
    expect(h.out()).toBe("the final answer never waits");
    expect(h.queue.pending()).toBe(0);
    expect(h.hasFrame()).toBe(false);
    h.queue.flush(); // idempotent — no empty apply
    expect(h.applied).not.toContain("");
  });

  it("reassembles text byte-identically across pushes (whitespace preserved)", () => {
    const h = harness();
    h.queue.push("Hello  world");
    h.queue.push("\n\nnext ¶ paragraph ");
    h.queue.push("\n"); // pure-whitespace delta must survive too (markdown breaks)
    h.queue.flush();
    expect(h.out()).toBe("Hello  world\n\nnext ¶ paragraph \n");
  });

  it("a push after the queue drained restarts the cadence — idle time never converts into a burst", () => {
    const h = harness({ msPerWord: 35 });
    h.queue.push("a");
    h.frame(16);
    expect(h.out()).toBe("a");
    h.idle(5000); // long tool call between text runs, no frames pending
    h.queue.push(" b c d");
    h.frame(16);
    // Only the pre-funded first chunk — the 5s of idle didn't bank 140 words.
    expect(h.out()).toBe("a b");
    expect(h.queue.pending()).toBe(2);
  });

  it("ignores empty pushes without scheduling a frame", () => {
    const h = harness();
    h.queue.push("");
    expect(h.hasFrame()).toBe(false);
    expect(h.queue.pending()).toBe(0);
  });

  it("exports the cadence inside the spec'd 30-60ms/word band", () => {
    expect(REVEAL_MS_PER_WORD).toBeGreaterThanOrEqual(30);
    expect(REVEAL_MS_PER_WORD).toBeLessThanOrEqual(60);
  });
});
