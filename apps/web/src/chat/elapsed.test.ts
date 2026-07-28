// jsdom + react-dom/client — the console has no @testing-library, and the unit harness is
// `.test.ts` only, so the hook is exercised through a probe component built with
// React.createElement rather than JSX (same shape as AuthGate.test.ts).
import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SHOW_ELAPSED_AFTER_MS, formatElapsed, useElapsed } from "./elapsed";

describe("formatElapsed", () => {
  it("reads as seconds under a minute", () => {
    expect(formatElapsed(0)).toBe("0s");
    expect(formatElapsed(2_000)).toBe("2s");
    expect(formatElapsed(59_999)).toBe("59s");
  });

  it("switches to minutes, zero-padded so the header width is stable", () => {
    expect(formatElapsed(60_000)).toBe("1m 00s");
    expect(formatElapsed(64_000)).toBe("1m 04s");
    expect(formatElapsed(3_599_000)).toBe("59m 59s");
  });

  it("switches to hours past sixty minutes", () => {
    expect(formatElapsed(3_600_000)).toBe("1h 00m");
    expect(formatElapsed(7_500_000)).toBe("2h 05m");
  });

  it("keeps the fifteen-minute case legible — the whole point", () => {
    // The DS `duration` formatter would render this as "900.4s".
    expect(formatElapsed(900_400)).toBe("15m 00s");
  });

  it("never renders negative time from a skewed clock", () => {
    expect(formatElapsed(-5_000)).toBe("0s");
  });
});

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("useElapsed", () => {
  let container: HTMLElement;
  let root: Root;
  let latest: number | undefined;

  function Probe({ at }: { at: number | undefined }) {
    latest = useElapsed(at);
    return null;
  }

  const render = (at: number | undefined) =>
    act(() => {
      root.render(h(Probe, { at }));
    });

  const tick = (ms: number) =>
    act(() => {
      vi.advanceTimersByTime(ms);
    });

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(1_000_000));
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  it("is undefined when nothing is running", () => {
    render(undefined);
    expect(latest).toBeUndefined();
    expect(vi.getTimerCount()).toBe(0); // a settled card must cost nothing
  });

  it("ticks about once a second while running", () => {
    render(1_000_000);
    expect(latest).toBe(0);

    tick(5_000);
    expect(latest).toBe(5_000);

    tick(900_000);
    expect(latest).toBe(905_000);
    expect(formatElapsed(latest!)).toBe("15m 05s");
  });

  it("resyncs when a newer tool takes the slot", () => {
    // The spotlight reuses ONE card instance as tools advance (a stable key, to avoid
    // remount strobe), so elapsed must restart from the NEW tool's start — not inherit
    // the previous card's clock.
    render(1_000_000);
    tick(30_000);
    expect(latest).toBe(30_000);

    render(1_030_000);
    expect(latest).toBe(0);
  });

  it("stops ticking once the call settles", () => {
    render(1_000_000);
    tick(3_000);
    render(undefined);
    expect(latest).toBeUndefined();
    expect(vi.getTimerCount()).toBe(0); // no interval left re-rendering a dead card
  });

  it("stays below the display threshold for a quick call", () => {
    render(1_000_000);
    tick(1_000);
    expect(latest! >= SHOW_ELAPSED_AFTER_MS).toBe(false);
    tick(1_500);
    expect(latest! >= SHOW_ELAPSED_AFTER_MS).toBe(true);
  });
});
