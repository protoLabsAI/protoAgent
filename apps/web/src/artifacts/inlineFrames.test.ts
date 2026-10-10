import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clampHeight,
  createFrameRegistry,
  LAZY_MOUNT_ROOT_MARGIN,
  MAX_FRAME_HEIGHT,
  MAX_LIVE_FRAMES,
  MIN_FRAME_HEIGHT,
  observeLazyMount,
} from "./inlineFrames";

// Inline frame cost bounds (ADR 0118 D2): the [80, 1200] px clamp, the 6-live-frame budget
// with least-recently-visible eviction, and the near-viewport lazy-mount wrapper.

describe("clampHeight", () => {
  it("clamps into [80, 1200] and floors every non-finite value to 80", () => {
    expect(clampHeight(500)).toBe(500);
    expect(clampHeight(MIN_FRAME_HEIGHT)).toBe(80);
    expect(clampHeight(MAX_FRAME_HEIGHT)).toBe(1200);
    // below the floor / above the ceiling
    expect(clampHeight(79)).toBe(80);
    expect(clampHeight(0)).toBe(80);
    expect(clampHeight(-40)).toBe(80);
    expect(clampHeight(5000)).toBe(1200);
    // non-finite floors to 80 — including +Infinity, which never reads as the ceiling
    expect(clampHeight(Number.NaN)).toBe(80);
    expect(clampHeight(Number.POSITIVE_INFINITY)).toBe(80);
    expect(clampHeight(Number.NEGATIVE_INFINITY)).toBe(80);
  });
});

describe("createFrameRegistry", () => {
  it("caps at 6 and the 7th registration evicts the least-recently-visible frame with its last height", () => {
    const reg = createFrameRegistry();
    expect(reg.cap).toBe(MAX_LIVE_FRAMES);

    // Six live frames, each reporting a distinct measured height.
    const heights = [100, 200, 300, 400, 500, 600];
    for (let i = 0; i < 6; i++) {
      expect(reg.register(`f${i}`)).toBeNull(); // room for all six
      reg.measure(`f${i}`, heights[i]);
    }
    expect(reg.size()).toBe(6);

    // f0 was registered first, so it is the oldest — until it scrolls back into view, which
    // bumps its recency and leaves f1 as the least-recently-visible frame.
    reg.touch("f0");

    // The 7th registration evicts f1 and reports f1's last measured height (200).
    const evicted = reg.register("f6");
    expect(evicted).toEqual({ id: "f1", height: 200 });
    expect(reg.size()).toBe(6);
    expect(reg.isLive("f1")).toBe(false);
    expect(reg.isLive("f6")).toBe(true);
    expect(reg.liveIds().sort()).toEqual(["f0", "f2", "f3", "f4", "f5", "f6"]);
  });

  it("re-registering a live frame evicts nothing and only refreshes its recency", () => {
    const reg = createFrameRegistry();
    for (let i = 0; i < 6; i++) reg.register(`f${i}`);

    // f0 is the oldest; re-registering it is a no-op that makes it the newest instead…
    expect(reg.register("f0")).toBeNull();
    expect(reg.size()).toBe(6);

    // …so the next eviction takes f1, not f0.
    expect(reg.register("f6")?.id).toBe("f1");
    expect(reg.isLive("f0")).toBe(true);
  });

  it("remembers (and clamps) a frame's last height for its resume card past eviction", () => {
    const reg = createFrameRegistry();
    reg.register("a");
    expect(reg.measure("a", 5000)).toBe(1200); // clamped on the way in
    reg.register("b");
    reg.release("a"); // frame unmounted — remembered height survives
    expect(reg.heightOf("a")).toBe(1200);
    expect(reg.heightOf("never-seen")).toBe(MIN_FRAME_HEIGHT);
  });

  it("reference-counts a shared id: the slot stays live until EVERY holder releases", () => {
    const reg = createFrameRegistry();
    // Two mounts of one component share an id (FrameComponentHostProps allows it).
    reg.register("shared"); // host A
    expect(reg.register("shared")).toBeNull(); // host B shares the slot — no new slot, no eviction
    expect(reg.size()).toBe(1);

    reg.release("shared"); // host A unmounts — host B still holds the slot
    expect(reg.isLive("shared")).toBe(true);
    expect(reg.size()).toBe(1);

    reg.release("shared"); // host B unmounts — last holder gone, slot freed
    expect(reg.isLive("shared")).toBe(false);
    expect(reg.size()).toBe(0);

    // Releasing again, or releasing an id that was never live, is a harmless no-op.
    expect(() => reg.release("shared")).not.toThrow();
    expect(() => reg.release("never")).not.toThrow();
    expect(reg.size()).toBe(0);
  });

  it("a shared id consumes ONE slot toward the cap, not one per holder", () => {
    const reg = createFrameRegistry();
    // Two holders of f0 plus five distinct ids = six live slots (f0 counts once).
    reg.register("f0");
    reg.register("f0");
    for (let i = 1; i < 6; i++) reg.register(`f${i}`);
    expect(reg.size()).toBe(6);

    // A seventh DISTINCT id still evicts exactly one to hold the cap.
    expect(reg.register("f6")).not.toBeNull();
    expect(reg.size()).toBe(6);
  });

  it("a stale release from an evicted holder never drops a re-registered sibling's slot", () => {
    const reg = createFrameRegistry(2);
    reg.register("x"); // holder A of x
    reg.register("x"); // holder B of x — x live, two claims
    reg.register("y"); // fills the cap (2)
    reg.register("z"); // evicts the least-recently-visible (x) — x's slot is gone, claims remain
    expect(reg.isLive("x")).toBe(false);

    // A newer holder C re-registers x, re-granting it a fresh live slot…
    reg.register("x");
    expect(reg.isLive("x")).toBe(true);

    // …and the two STALE releases from the evicted A/B must not kill C's slot.
    reg.release("x"); // was A
    reg.release("x"); // was B
    expect(reg.isLive("x")).toBe(true);

    // Only C's own release frees it.
    reg.release("x");
    expect(reg.isLive("x")).toBe(false);
  });
});

// A mock IntersectionObserver: it records what it observes and lets a test drive the callback.
class MockIntersectionObserver {
  static instances: MockIntersectionObserver[] = [];
  callback: IntersectionObserverCallback;
  rootMargin: string;
  observed: Element[] = [];
  disconnected = false;

  constructor(cb: IntersectionObserverCallback, options?: IntersectionObserverInit) {
    this.callback = cb;
    this.rootMargin = options?.rootMargin ?? "0px";
    MockIntersectionObserver.instances.push(this);
  }
  observe(el: Element) {
    this.observed.push(el);
  }
  unobserve(el: Element) {
    this.observed = this.observed.filter((e) => e !== el);
  }
  disconnect() {
    this.disconnected = true;
  }
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
  /** Simulate the observer firing for everything it watches. */
  fire(isIntersecting: boolean) {
    const entries = this.observed.map((target) => ({ target, isIntersecting }) as IntersectionObserverEntry);
    this.callback(entries, this as unknown as IntersectionObserver);
  }
}

describe("observeLazyMount", () => {
  const realIO = globalThis.IntersectionObserver;

  beforeEach(() => {
    MockIntersectionObserver.instances = [];
    globalThis.IntersectionObserver = MockIntersectionObserver as unknown as typeof IntersectionObserver;
  });
  afterEach(() => {
    globalThis.IntersectionObserver = realIO;
    vi.restoreAllMocks();
  });

  it("fires once when the frame scrolls near the viewport, then stops observing", () => {
    const el = document.createElement("div");
    const onEnter = vi.fn();
    observeLazyMount(el, onEnter);

    const { instances } = MockIntersectionObserver;
    const io = instances[instances.length - 1];
    expect(io.observed).toContain(el);
    expect(io.rootMargin).toBe(LAZY_MOUNT_ROOT_MARGIN); // mounts just BEFORE it's on screen
    expect(onEnter).not.toHaveBeenCalled();

    // Off screen → still not mounted.
    io.fire(false);
    expect(onEnter).not.toHaveBeenCalled();

    // Scrolls into (near) view → mounts exactly once and disconnects.
    io.fire(true);
    expect(onEnter).toHaveBeenCalledTimes(1);
    expect(io.disconnected).toBe(true);

    // A later intersection does not re-fire.
    io.fire(true);
    expect(onEnter).toHaveBeenCalledTimes(1);
  });

  it("mounts eagerly where IntersectionObserver is unavailable", () => {
    globalThis.IntersectionObserver = undefined as unknown as typeof IntersectionObserver;
    const onEnter = vi.fn();
    const handle = observeLazyMount(document.createElement("div"), onEnter);
    expect(onEnter).toHaveBeenCalledTimes(1);
    expect(() => handle.disconnect()).not.toThrow();
  });
});
