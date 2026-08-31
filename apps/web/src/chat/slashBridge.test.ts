// The imperative seam a client slash command is dispatched through from OUTSIDE the chat
// composer (ADR 0061). Same contract as escapeStop's handler seam — last-write-wins,
// guarded unregister, inert when nothing is registered — plus the two facts a caller needs
// before it offers a command: is a slot mounted, and does it have a session (13 of the 16
// core commands decline a null session, and from outside there is no draft to fall through
// to, so a caller must be able to see that BEFORE dispatching).
import { afterEach, describe, expect, it, vi } from "vitest";

import { registerSlashDispatcher, runSlashFromOutside, slashDispatchTarget } from "./slashBridge";

// The seam is module state; every test registers explicitly, so clear any leak between them.
const offs: (() => void)[] = [];
const register = (dispatcher: Parameters<typeof registerSlashDispatcher>[0]) => {
  const off = registerSlashDispatcher(dispatcher);
  offs.push(off);
  return off;
};
afterEach(() => {
  while (offs.length) offs.pop()?.();
});

describe("slash dispatch seam", () => {
  it("returns false with no dispatcher registered — the caller falls back, nothing is swallowed", () => {
    expect(runSlashFromOutside("help")).toBe(false);
    expect(slashDispatchTarget()).toBeNull();
  });

  it("dispatches the raw command to the registered slot", () => {
    const run = vi.fn(() => true);
    register({ run, sessionId: "s1" });
    expect(runSlashFromOutside("effort high")).toBe(true);
    expect(run).toHaveBeenCalledWith("effort high");
  });

  it("tolerates the leading slash so a display token can't read as 'no chat surface'", () => {
    const run = vi.fn(() => true);
    register({ run, sessionId: "s1" });
    expect(runSlashFromOutside("/help")).toBe(true);
    expect(run).toHaveBeenCalledWith("help");
  });

  it("passes a command's own false through — declining is NOT the same as unhandled to the slot, but both mean 'fall back'", () => {
    // e.g. /clear with no session, or a flag-off command: runClientSlash returns false.
    register({ run: () => false, sessionId: null });
    expect(runSlashFromOutside("clear")).toBe(false);
  });

  it("never dispatches an empty token", () => {
    const run = vi.fn(() => true);
    register({ run, sessionId: "s1" });
    expect(runSlashFromOutside("   ")).toBe(false);
    expect(runSlashFromOutside("/")).toBe(false);
    expect(run).not.toHaveBeenCalled();
  });

  it("last write wins — the newly visible slot replaces the outgoing one", () => {
    const a = vi.fn(() => true);
    const b = vi.fn(() => true);
    register({ run: a, sessionId: "a" });
    register({ run: b, sessionId: "b" });
    runSlashFromOutside("help");
    expect(a).not.toHaveBeenCalled();
    expect(b).toHaveBeenCalledTimes(1);
    expect(slashDispatchTarget()).toEqual({ sessionId: "b" });
  });

  it("a stale unregister never drops a fresh registration (cleanup-order safety)", () => {
    const a = vi.fn(() => true);
    const b = vi.fn(() => true);
    const offA = register({ run: a, sessionId: "a" });
    register({ run: b, sessionId: "b" });
    // Slot A's effect cleanup can run AFTER slot B registered (React re-render ordering) —
    // it must only clear its own target.
    offA();
    expect(runSlashFromOutside("help")).toBe(true);
    expect(b).toHaveBeenCalledTimes(1);
    expect(slashDispatchTarget()).toEqual({ sessionId: "b" });
  });

  it("unregistering the live target makes the seam inert again", () => {
    const run = vi.fn(() => true);
    const off = register({ run, sessionId: "s1" });
    off();
    expect(runSlashFromOutside("help")).toBe(false);
    expect(run).not.toHaveBeenCalled();
    expect(slashDispatchTarget()).toBeNull();
  });
});

describe("slashDispatchTarget — what a caller must check before offering a command", () => {
  it("null means no chat slot is mounted at all", () => {
    expect(slashDispatchTarget()).toBeNull();
  });

  it("reports a mounted slot that has NO session — session-scoped commands will decline", () => {
    register({ run: () => false, sessionId: null });
    expect(slashDispatchTarget()).toEqual({ sessionId: null });
  });

  it("reports the visible slot's session id", () => {
    register({ run: () => true, sessionId: "sess-42" });
    expect(slashDispatchTarget()).toEqual({ sessionId: "sess-42" });
  });

  it("tracks the CURRENT registration, not the first one (per-render re-registration)", () => {
    register({ run: () => true, sessionId: "sess-1" });
    register({ run: () => true, sessionId: "sess-2" });
    expect(slashDispatchTarget()).toEqual({ sessionId: "sess-2" });
  });
});
