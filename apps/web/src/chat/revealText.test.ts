// Progressive reveal for a complete background reply (#4247). Pure-plan tests run directly;
// the hook is driven through the same minimal createRoot/act renderHook the other console
// hook suites use (no testing-library dep), with an injected clock + captured rAF so the
// reveal steps deterministically. The reducer-marks case exercises the real
// applyProgressFrame so the mark-once / dedupe contract can't drift from the reducer.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { applyProgressFrame, type ProgressFrame } from "../app/serverTurnProgress";
import {
  _resetReveals,
  markForReveal,
  nextRevealLength,
  REVEAL_CHARS_PER_SEC,
  REVEAL_MAX_MS,
  REVEAL_MIN_MS,
  revealPlan,
  takeReveal,
  useRevealedText,
} from "./revealText";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// ── Pure plan ─────────────────────────────────────────────────────────────────────────────

describe("revealPlan / nextRevealLength", () => {
  it("only ever cuts at word boundaries (never mid-word)", () => {
    const text = "the quick brown fox jumps";
    const { stops } = revealPlan(text);
    // Every interior stop lands just past the whitespace that follows a word.
    for (const s of stops.slice(0, -1)) expect(/\s/.test(text[s - 1])).toBe(true);
    expect(stops[stops.length - 1]).toBe(text.length);
    // Sweeping elapsed time, the revealed length is always a plan stop (or 0 before the first).
    const { durationMs } = revealPlan(text);
    for (let e = 0; e <= durationMs + 50; e += 17) {
      const n = nextRevealLength(text, e);
      expect(n === 0 || stops.includes(n)).toBe(true);
      expect(text.startsWith(text.slice(0, n))).toBe(true);
    }
  });

  it("never cuts inside a ``` fence marker", () => {
    const text = "Here is code:\n```js\nconst x = 1;\n```\nDone.";
    for (const s of revealPlan(text).stops) {
      // A cut never falls between two backticks of a fence run.
      expect(text[s - 1] === "`" && text[s] === "`").toBe(false);
    }
  });

  it("clamps the target duration for short and very long text", () => {
    expect(revealPlan("hi there").durationMs).toBe(REVEAL_MIN_MS); // 8 chars → floor
    expect(revealPlan("word ".repeat(1000)).durationMs).toBe(REVEAL_MAX_MS); // 5000 chars → ceil
    // A mid-length text scales ~len/600 without clamping.
    expect(revealPlan("x".repeat(1200)).durationMs).toBe((1200 / REVEAL_CHARS_PER_SEC) * 1000);
  });

  it("finishes within the clamped duration, and not before the floor for non-trivial text", () => {
    const long = "word ".repeat(1000);
    expect(nextRevealLength(long, REVEAL_MAX_MS)).toBe(long.length);
    expect(nextRevealLength(long, REVEAL_MAX_MS + 500)).toBe(long.length);
    const text = "alpha beta gamma delta";
    expect(nextRevealLength(text, REVEAL_MIN_MS - 1)).toBeLessThan(text.length);
    expect(nextRevealLength(text, REVEAL_MIN_MS)).toBe(text.length);
  });

  it("reveals nothing from empty text", () => {
    expect(nextRevealLength("", 0)).toBe(0);
    expect(nextRevealLength("", 9999)).toBe(0);
  });
});

// ── Reducer marking ─────────────────────────────────────────────────────────────────────────

const roomFrame = (id: string): ProgressFrame => ({
  session: "s1",
  taskId: "t1",
  kind: "room",
  id,
  author: "proto",
  text: "a background reply",
  ok: true,
});

describe("applyProgressFrame marks a room reply for reveal", () => {
  beforeEach(() => _resetReveals());

  it("marks a newly-appended room message once; a duplicate frame does not re-mark it", () => {
    const once = applyProgressFrame([], roomFrame("bg-1"));
    expect(takeReveal("background-room-bg-1")).toBe(true); // marked on the fresh append
    const twice = applyProgressFrame(once, roomFrame("bg-1"));
    expect(twice).toBe(once); // dedupe: identical array, reducer stays pure
    expect(takeReveal("background-room-bg-1")).toBe(false); // the dupe did NOT re-mark
  });
});

// ── The hook ──────────────────────────────────────────────────────────────────────────────

type HookResult = ReturnType<typeof useRevealedText>;

let container: HTMLElement;
let root: Root;
let result: { current: HookResult };
let Probe: (props: { id: string; text: string }) => null;

let now = 0;
let scheduled: FrameRequestCallback | null = null;
const realRaf = window.requestAnimationFrame;
const realCaf = window.cancelAnimationFrame;
const realMatchMedia = window.matchMedia;

function setReducedMotion(on: boolean) {
  window.matchMedia = ((q: string) => ({
    matches: on && /prefers-reduced-motion/.test(q),
    media: q,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

/** Advance the clock by `ms` and fire the one pending frame, like a browser frame arriving. */
function frame(ms: number) {
  now += ms;
  const cb = scheduled;
  scheduled = null;
  if (cb) act(() => cb(now));
}

function mount(id: string, text: string) {
  result = { current: { shown: "", revealing: false } };
  Probe = function Probe(props: { id: string; text: string }) {
    result.current = useRevealedText(props.id, props.text);
    return null;
  };
  act(() => root.render(h(Probe, { id, text })));
  return result;
}

describe("useRevealedText", () => {
  beforeEach(() => {
    _resetReveals();
    now = 0;
    scheduled = null;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    window.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      scheduled = cb;
      return 1;
    }) as unknown as typeof window.requestAnimationFrame;
    window.cancelAnimationFrame = (() => {
      scheduled = null;
    }) as unknown as typeof window.cancelAnimationFrame;
    setReducedMotion(false);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    window.requestAnimationFrame = realRaf;
    window.cancelAnimationFrame = realCaf;
    window.matchMedia = realMatchMedia;
  });

  it("grows a marked message from empty to full, cutting only at word boundaries", () => {
    markForReveal("m1");
    const text = "one two three four five six";
    const r = mount("m1", text);
    expect(r.current.shown).toBe(""); // nothing shown at t=0
    expect(r.current.revealing).toBe(true);

    frame(300); // half of the 600ms plan
    expect(r.current.shown.length).toBeGreaterThan(0);
    expect(r.current.shown.length).toBeLessThan(text.length);
    expect(text.startsWith(r.current.shown)).toBe(true);
    expect(/\s$/.test(r.current.shown)).toBe(true); // ended on a word boundary, not mid-word
    expect(r.current.revealing).toBe(true);

    frame(400); // past the clamped duration
    expect(r.current.shown).toBe(text);
    expect(r.current.revealing).toBe(false);
    expect(scheduled).toBeNull(); // loop stopped — no leaked frame
  });

  it("renders the full text immediately when the id was never marked", () => {
    const r = mount("unmarked", "hello there world");
    expect(r.current.shown).toBe("hello there world");
    expect(r.current.revealing).toBe(false);
    expect(scheduled).toBeNull(); // no animation scheduled
  });

  it("renders the full text immediately under prefers-reduced-motion", () => {
    setReducedMotion(true);
    markForReveal("m2");
    const r = mount("m2", "hello there world");
    expect(r.current.shown).toBe("hello there world");
    expect(r.current.revealing).toBe(false);
    expect(scheduled).toBeNull();
  });

  it("renders immediately when rendered again after the mark was consumed", () => {
    markForReveal("m3");
    const first = mount("m3", "hello there world");
    expect(first.current.revealing).toBe(true); // the mark is consumed here
    const second = mount("m3", "hello there world"); // a fresh mount; the mark is gone
    expect(second.current.shown).toBe("hello there world");
    expect(second.current.revealing).toBe(false);
  });
});
