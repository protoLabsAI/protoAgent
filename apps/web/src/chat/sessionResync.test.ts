import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import {
  RESYNC_INTERVAL_MS,
  RESYNC_MIN_GAP_MS,
  resetDurableResyncForTests,
  resyncDurableChatSessions,
  watchDurableSessions,
} from "./sessionHydration";

// An open console used to hydrate once at boot and never again, so a desktop window left
// open all day never showed the replies sent from the phone (ADR 0104 multi-device).

// Each hydration reads the index twice (newest + parked).
const indexReads = (spy: { mock: { calls: unknown[] } }) => spy.mock.calls.length / 2;

function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
}

describe("an open console keeps re-syncing durable sessions", () => {
  let sessions: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.useFakeTimers();
    resetDurableResyncForTests();
    setVisibility("visible");
    sessions = vi.spyOn(api, "chatSessions").mockResolvedValue({ sessions: [] });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    setVisibility("visible");
  });

  it("is single-flight and spaced, unless forced", async () => {
    const t0 = 1_000_000;
    await resyncDurableChatSessions({ now: t0 });
    await resyncDurableChatSessions({ now: t0 + 1 }); // inside the gap: skipped
    expect(indexReads(sessions)).toBe(1);
    await resyncDurableChatSessions({ now: t0 + RESYNC_MIN_GAP_MS });
    expect(indexReads(sessions)).toBe(2);
    await resyncDurableChatSessions({ now: t0 + RESYNC_MIN_GAP_MS + 1, force: true });
    expect(indexReads(sessions)).toBe(3);

    // A second call while one is out joins it rather than starting another.
    const pending: Array<() => void> = [];
    sessions.mockImplementation(() => new Promise((resolve) => pending.push(() => resolve({ sessions: [] }))));
    const a = resyncDurableChatSessions({ force: true });
    const b = resyncDurableChatSessions({ force: true });
    expect(a).toBe(b);
    pending.forEach((release) => release());
    await a;
  });

  it("re-syncs when the window comes back and on the interval while visible, never while hidden", async () => {
    const stop = watchDurableSessions();
    try {
      window.dispatchEvent(new Event("focus"));
      await vi.advanceTimersByTimeAsync(0);
      expect(indexReads(sessions)).toBe(1);

      await vi.advanceTimersByTimeAsync(RESYNC_INTERVAL_MS);
      expect(indexReads(sessions)).toBe(2);

      setVisibility("hidden");
      await vi.advanceTimersByTimeAsync(RESYNC_INTERVAL_MS * 3);
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
      expect(indexReads(sessions)).toBe(2);

      setVisibility("visible");
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
      expect(indexReads(sessions)).toBe(3);
    } finally {
      stop();
    }
    await vi.advanceTimersByTimeAsync(RESYNC_INTERVAL_MS * 2);
    expect(indexReads(sessions)).toBe(3);
  });
});
