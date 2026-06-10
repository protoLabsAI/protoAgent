import { describe, it, expect } from "vitest";
import { ensureActiveSessions, MAX_ACTIVE_SESSIONS, type ChatState } from "./chat-store";

// ensureActiveSessions is the LRU that decides which chat sessions stay mounted.
// Its load-bearing invariant: it must NEVER evict a session that is mid-stream
// while a non-streaming session exists to drop instead — evicting a streaming
// session would unmount it and lose the in-flight response.

function mkState(activeSessions: string[], streaming: string[] = []): ChatState {
  const sessionStatusMap: Record<string, "idle" | "streaming" | "error"> = {};
  for (const id of streaming) sessionStatusMap[id] = "streaming";
  return { activeSessions, sessionStatusMap } as unknown as ChatState;
}

describe("ensureActiveSessions", () => {
  it("returns the existing list unchanged for a null sessionId", () => {
    const state = mkState(["a", "b"]);
    expect(ensureActiveSessions(state, null)).toBe(state.activeSessions);
  });

  it("is a no-op when the session is already active", () => {
    const state = mkState(["a", "b"]);
    expect(ensureActiveSessions(state, "b")).toBe(state.activeSessions);
  });

  it("appends without eviction while under the cap", () => {
    const state = mkState(["a", "b"]);
    expect(ensureActiveSessions(state, "c")).toEqual(["a", "b", "c"]);
  });

  it("appends up to exactly the cap without eviction", () => {
    const ids = Array.from({ length: MAX_ACTIVE_SESSIONS - 1 }, (_, i) => `s${i}`);
    const state = mkState(ids);
    const next = ensureActiveSessions(state, "new");
    expect(next).toHaveLength(MAX_ACTIVE_SESSIONS);
    expect(next).toContain("new");
  });

  it("evicts the oldest session when over the cap (all idle)", () => {
    const full = Array.from({ length: MAX_ACTIVE_SESSIONS }, (_, i) => `s${i}`);
    const next = ensureActiveSessions(mkState(full), "new");
    expect(next).toHaveLength(MAX_ACTIVE_SESSIONS);
    expect(next[0]).toBe("s1"); // s0 (oldest) dropped
    expect(next).toContain("new");
    expect(next).not.toContain("s0");
  });

  it("NEVER evicts a streaming session while a non-streaming one exists", () => {
    // s0 is the oldest but mid-stream → the next non-streaming session (s1) goes instead.
    const full = ["s0", "s1", "s2", "s3", "s4"];
    const next = ensureActiveSessions(mkState(full, ["s0"]), "new");
    expect(next).toContain("s0"); // streaming session preserved
    expect(next).not.toContain("s1"); // oldest non-streaming evicted instead
    expect(next).toContain("new");
    expect(next).toHaveLength(MAX_ACTIVE_SESSIONS);
  });

  it("never evicts the just-added session even if everything else streams", () => {
    const full = ["s0", "s1", "s2", "s3", "s4"];
    const next = ensureActiveSessions(mkState(full, full), "new");
    expect(next).toContain("new");
    expect(next).toHaveLength(MAX_ACTIVE_SESSIONS);
  });

  it("falls back to dropping the oldest when every other session streams", () => {
    const full = ["s0", "s1", "s2", "s3", "s4"];
    const next = ensureActiveSessions(mkState(full, full), "new");
    expect(next[0]).toBe("s1"); // s0 dropped despite streaming — no non-streaming option
    expect(next).not.toContain("s0");
  });
});
