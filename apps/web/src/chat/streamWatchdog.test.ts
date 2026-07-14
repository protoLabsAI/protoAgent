import { describe, expect, it, vi } from "vitest";

import { createStreamWatchdog, isTerminalTaskState, type WatchdogTaskState } from "./streamWatchdog";

// Deterministic single-shot timer stand-in matching setTimeout/clearTimeout shape.
// `fire()` elapses the currently-armed idle timeout and flushes the async onIdle.
function fakeClock() {
  let pending: { id: number; fn: () => void } | null = null;
  let nextId = 1;
  return {
    setTimer: (fn: () => void) => {
      pending = { id: nextId, fn };
      return nextId++ as unknown as ReturnType<typeof setTimeout>;
    },
    clearTimer: (h: ReturnType<typeof setTimeout>) => {
      if (pending && pending.id === (h as unknown as number)) pending = null;
    },
    armed: () => pending !== null,
    async fire() {
      const p = pending;
      pending = null;
      if (p) await p.fn();
    },
  };
}

const IDLE = 45_000;

describe("isTerminalTaskState", () => {
  it("treats completed/failed/canceled and a missing state as terminal", () => {
    for (const s of ["TASK_STATE_COMPLETED", "completed", "failed", "canceled", "cancelled", ""]) {
      expect(isTerminalTaskState(s)).toBe(true);
    }
  });
  it("treats an in-flight state as non-terminal", () => {
    for (const s of ["TASK_STATE_WORKING", "working", "submitted", "input-required"]) {
      expect(isTerminalTaskState(s)).toBe(false);
    }
  });
});

describe("createStreamWatchdog", () => {
  it("self-heals a stalled stream: fires onTerminal from the durable task", async () => {
    const clock = fakeClock();
    const onTerminal = vi.fn();
    const task: WatchdogTaskState = { state: "TASK_STATE_COMPLETED", text: "the recovered answer" };
    const wd = createStreamWatchdog({
      idleMs: IDLE,
      getTask: async () => task,
      onTerminal,
      setTimer: clock.setTimer,
      clearTimer: clock.clearTimer,
    });

    wd.bump(); // stream started
    expect(clock.armed()).toBe(true);
    await clock.fire(); // idle window elapsed with no frames

    expect(onTerminal).toHaveBeenCalledTimes(1);
    expect(onTerminal).toHaveBeenCalledWith(task);
    expect(wd.settled()).toBe(true);
    expect(clock.armed()).toBe(false); // does not re-arm once settled
  });

  it("keeps waiting when the task is still working (no false finalize)", async () => {
    const clock = fakeClock();
    const onTerminal = vi.fn();
    const wd = createStreamWatchdog({
      idleMs: IDLE,
      getTask: async () => ({ state: "TASK_STATE_WORKING", text: "" }),
      onTerminal,
      setTimer: clock.setTimer,
      clearTimer: clock.clearTimer,
    });

    wd.bump();
    await clock.fire(); // idle — but the server is genuinely still working

    expect(onTerminal).not.toHaveBeenCalled();
    expect(wd.settled()).toBe(false);
    expect(clock.armed()).toBe(true); // re-armed to keep watching
  });

  it("retries (re-arms) when the task can't be fetched yet", async () => {
    const clock = fakeClock();
    const onTerminal = vi.fn();
    const wd = createStreamWatchdog({
      idleMs: IDLE,
      getTask: async () => {
        throw new Error("task id not surfaced yet");
      },
      onTerminal,
      setTimer: clock.setTimer,
      clearTimer: clock.clearTimer,
    });

    wd.bump();
    await clock.fire();

    expect(onTerminal).not.toHaveBeenCalled();
    expect(wd.settled()).toBe(false);
    expect(clock.armed()).toBe(true);
  });

  it("bump resets the countdown rather than stacking timers", () => {
    const clock = fakeClock();
    const wd = createStreamWatchdog({
      idleMs: IDLE,
      getTask: async () => ({ state: "", text: "" }),
      onTerminal: vi.fn(),
      setTimer: clock.setTimer,
      clearTimer: clock.clearTimer,
    });
    wd.bump();
    wd.bump();
    wd.bump();
    expect(clock.armed()).toBe(true); // exactly one pending timer, not three
  });

  it("stop() disarms and makes bump a no-op", async () => {
    const clock = fakeClock();
    const onTerminal = vi.fn();
    const wd = createStreamWatchdog({
      idleMs: IDLE,
      getTask: async () => ({ state: "TASK_STATE_COMPLETED", text: "x" }),
      onTerminal,
      setTimer: clock.setTimer,
      clearTimer: clock.clearTimer,
    });
    wd.bump();
    wd.stop(); // stream closed normally / errored / unmounted
    expect(clock.armed()).toBe(false);
    wd.bump(); // must not re-arm after stop
    expect(clock.armed()).toBe(false);
    await clock.fire(); // nothing pending; onTerminal never fires
    expect(onTerminal).not.toHaveBeenCalled();
  });
});
