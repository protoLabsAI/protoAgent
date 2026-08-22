// #2968 — Escape-to-stop behavior (the Claude.ai/ChatGPT convention). These pin the
// per-press decision — streaming + queued steers peels the NEWEST steer (LIFO, one per
// press), streaming with none stops the turn, idle is a strict no-op — and the imperative
// seam ChatSurface publishes its handler on (last-write-wins + guarded unregister, so the
// per-render re-registration and slot-swap cleanup ordering can't strand or drop a handler).
import { describe, expect, it, vi } from "vitest";

import { registerChatEscapeHandler, resolveEscapeAction, runChatEscape } from "./escapeStop";

describe("resolveEscapeAction", () => {
  it("is a no-op while idle — even with (stale) steers around", () => {
    expect(resolveEscapeAction("idle", [])).toEqual({ kind: "none" });
    expect(resolveEscapeAction("idle", [{ id: "s1" }])).toEqual({ kind: "none" });
  });

  it("is a no-op on an errored session", () => {
    expect(resolveEscapeAction("error", [{ id: "s1" }])).toEqual({ kind: "none" });
  });

  it("stops the turn while streaming with no queued steers", () => {
    expect(resolveEscapeAction("streaming", [])).toEqual({ kind: "stop" });
  });

  it("cancels the NEWEST queued steer while streaming (LIFO)", () => {
    expect(resolveEscapeAction("streaming", [{ id: "s1" }, { id: "s2" }, { id: "s3" }])).toEqual({
      kind: "cancel-steer",
      steerId: "s3",
    });
  });

  it("successive presses peel one steer at a time, then stop the turn", () => {
    let queue = [{ id: "s1" }, { id: "s2" }];
    const peeled: string[] = [];
    for (;;) {
      const action = resolveEscapeAction("streaming", queue);
      if (action.kind !== "cancel-steer") {
        expect(action).toEqual({ kind: "stop" });
        break;
      }
      peeled.push(action.steerId);
      queue = queue.filter((q) => q.id !== action.steerId);
    }
    expect(peeled).toEqual(["s2", "s1"]); // newest first, exactly one per press
  });
});

describe("chat escape handler seam", () => {
  it("runChatEscape with no registered handler is a no-op (chat surface unmounted)", () => {
    expect(() => runChatEscape()).not.toThrow();
  });

  it("invokes the registered handler", () => {
    const handler = vi.fn();
    const off = registerChatEscapeHandler(handler);
    runChatEscape();
    expect(handler).toHaveBeenCalledTimes(1);
    off();
    runChatEscape();
    expect(handler).toHaveBeenCalledTimes(1); // unregistered → no-op again
  });

  it("last write wins — the newly visible slot replaces the outgoing one", () => {
    const a = vi.fn();
    const b = vi.fn();
    const offA = registerChatEscapeHandler(a);
    const offB = registerChatEscapeHandler(b);
    runChatEscape();
    expect(a).not.toHaveBeenCalled();
    expect(b).toHaveBeenCalledTimes(1);
    offA();
    offB();
  });

  it("a stale unregister never drops a fresh registration (cleanup-order safety)", () => {
    const a = vi.fn();
    const b = vi.fn();
    const offA = registerChatEscapeHandler(a);
    const offB = registerChatEscapeHandler(b);
    // Slot A's effect cleanup can run AFTER slot B registered (React re-render ordering) —
    // it must only clear its own handler.
    offA();
    runChatEscape();
    expect(b).toHaveBeenCalledTimes(1);
    offB();
    runChatEscape();
    expect(b).toHaveBeenCalledTimes(1);
  });
});
