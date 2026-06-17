import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  ensureActiveSessions,
  sanitizePersisted,
  MAX_ACTIVE_SESSIONS,
  type ChatSession,
  type ChatState,
} from "./chat-store";

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

// Persistence debouncing: the server flushes SSE every ~24 chars and every frame
// lands in updateMessages, so the localStorage write (full-store serialize +
// cross-window `storage` event) must NOT run per frame. Streamed updates write
// on a trailing timer; structural changes and unload flush immediately. The
// in-memory snapshot must still update synchronously (the UI streams live).

const msg = (content: string) =>
  [{ id: "m1", role: "assistant" as const, content }] as never[];

describe("persist debouncing", () => {
  let setItem: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.useFakeTimers();
    window.localStorage.clear();
    vi.resetModules(); // fresh module-level store + timer state per test
    setItem = vi.spyOn(Storage.prototype, "setItem");
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    setItem.mockRestore();
  });

  async function freshStore() {
    const mod = await import("./chat-store");
    setItem.mockClear(); // ignore writes from module init / setup calls
    return mod;
  }

  it("coalesces many rapid streamed updates into ONE write", async () => {
    const { chatStore, PERSIST_DEBOUNCE_MS } = await freshStore();
    const sessionId = chatStore.getSnapshot().currentSessionId!;

    for (let i = 0; i < 50; i++) {
      chatStore.updateMessages(sessionId, msg("token ".repeat(i + 1)));
    }
    expect(setItem).not.toHaveBeenCalled(); // nothing synchronous

    vi.advanceTimersByTime(PERSIST_DEBOUNCE_MS);
    expect(setItem).toHaveBeenCalledTimes(1); // one trailing write

    // …and it wrote the LATEST state, not the first frame.
    const written = JSON.parse(setItem.mock.calls[0][1] as string);
    const session = written.sessions.find((s: { id: string }) => s.id === sessionId);
    expect(session.messages[0].content).toBe("token ".repeat(50));
  });

  it("keeps the in-memory snapshot synchronous while the write is pending", async () => {
    const { chatStore } = await freshStore();
    const sessionId = chatStore.getSnapshot().currentSessionId!;

    chatStore.updateMessages(sessionId, msg("live"));
    const session = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)!;
    expect(session.messages[0]).toMatchObject({ content: "live" }); // before any timer fires
    expect(setItem).not.toHaveBeenCalled();
  });

  it("flushes immediately on structural changes (create/delete/rename/switch)", async () => {
    const { chatStore } = await freshStore();
    const sessionId = chatStore.getSnapshot().currentSessionId!;

    chatStore.updateMessages(sessionId, msg("pending")); // debounced…
    const created = chatStore.createSession(); // …structural change flushes NOW
    expect(setItem).toHaveBeenCalledTimes(1);
    const written = JSON.parse(setItem.mock.calls[0][1] as string);
    expect(written.sessions.map((s: { id: string }) => s.id)).toContain(created.id);
    // The pending streamed content rode along with the flush.
    const session = written.sessions.find((s: { id: string }) => s.id === sessionId);
    expect(session.messages[0].content).toBe("pending");

    chatStore.renameSession(created.id, "named");
    expect(setItem).toHaveBeenCalledTimes(2);
    chatStore.switchSession(sessionId);
    expect(setItem).toHaveBeenCalledTimes(3);
    chatStore.deleteSession(created.id);
    expect(setItem).toHaveBeenCalledTimes(4);
  });

  it("reorderSessions reorders the tabs, preserves the active session, and flushes immediately", async () => {
    const { chatStore } = await freshStore();
    const first = chatStore.getSnapshot().currentSessionId!;
    const b = chatStore.createSession();
    const c = chatStore.createSession();
    chatStore.switchSession(first); // active = first; order is [first, b, c]
    setItem.mockClear();

    chatStore.reorderSessions([c.id, first, b.id]); // drag c to the front
    expect(chatStore.getSnapshot().sessions.map((s) => s.id)).toEqual([c.id, first, b.id]);
    expect(chatStore.getSnapshot().currentSessionId).toBe(first); // active untouched
    expect(setItem).toHaveBeenCalledTimes(1); // structural → immediate flush
  });

  it("reorderSessions keeps a session the caller omitted (defensive)", async () => {
    const { chatStore } = await freshStore();
    const first = chatStore.getSnapshot().currentSessionId!;
    const b = chatStore.createSession();

    chatStore.reorderSessions([b.id]); // omit `first`
    expect(chatStore.getSnapshot().sessions.map((s) => s.id)).toEqual([b.id, first]);
  });

  it("flushes on stream done (setSessionStatus) so the final answer persists", async () => {
    const { chatStore } = await freshStore();
    const sessionId = chatStore.getSnapshot().currentSessionId!;

    chatStore.updateMessages(sessionId, msg("final answer"));
    chatStore.setSessionStatus(sessionId, "idle"); // ChatSurface's stream-done path
    expect(setItem).toHaveBeenCalledTimes(1);
    const written = JSON.parse(setItem.mock.calls[0][1] as string);
    const session = written.sessions.find((s: { id: string }) => s.id === sessionId);
    expect(session.messages[0].content).toBe("final answer");

    // No stale trailing write after the flush.
    vi.runOnlyPendingTimers();
    expect(setItem).toHaveBeenCalledTimes(1);
  });

  it("setSessionModel sets a per-tab model and clears it on empty", async () => {
    const { chatStore } = await freshStore();
    const sessionId = chatStore.getSnapshot().currentSessionId!;
    const modelOf = () => chatStore.getSnapshot().sessions.find((s) => s.id === sessionId)!.model;

    chatStore.setSessionModel(sessionId, "protolabs/fast");
    expect(modelOf()).toBe("protolabs/fast");
    chatStore.setSessionModel(sessionId, ""); // clear → back to the configured default
    expect(modelOf()).toBeUndefined();
  });

  it("flushes pending state on pagehide/beforeunload, and only when dirty", async () => {
    const { chatStore } = await freshStore();
    const sessionId = chatStore.getSnapshot().currentSessionId!;

    chatStore.updateMessages(sessionId, msg("about to navigate"));
    window.dispatchEvent(new Event("pagehide"));
    expect(setItem).toHaveBeenCalledTimes(1);

    // Clean store → unload is a no-op (a tenant-clear must never be re-written).
    window.dispatchEvent(new Event("pagehide"));
    window.dispatchEvent(new Event("beforeunload"));
    expect(setItem).toHaveBeenCalledTimes(1);
  });

  it("flushChatPersist is a no-op when nothing is pending", async () => {
    const { flushChatPersist } = await freshStore();
    flushChatPersist();
    expect(setItem).not.toHaveBeenCalled();
  });
});

// sanitizePersisted is the pure half of loadPersisted (#872): a corrupt or
// hand-edited localStorage blob must never reach render — drop the bad
// sessions, keep the good ones, and re-point currentSessionId if its target
// was dropped. null = nothing usable (the caller starts a fresh session).

function mkSession(id: string, overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    id,
    title: `chat ${id}`,
    messages: [{ role: "user", content: "hi" }],
    createdAt: 1,
    updatedAt: 1,
    ...overrides,
  };
}

describe("sanitizePersisted", () => {
  it("returns null for non-objects and shapeless blobs", () => {
    expect(sanitizePersisted(null)).toBeNull();
    expect(sanitizePersisted("corrupt")).toBeNull();
    expect(sanitizePersisted(42)).toBeNull();
    expect(sanitizePersisted({})).toBeNull();
    expect(sanitizePersisted({ sessions: "not-an-array" })).toBeNull();
  });

  it("passes a valid blob through intact", () => {
    const blob = { version: 1, sessions: [mkSession("a"), mkSession("b")], currentSessionId: "b" };
    expect(sanitizePersisted(blob)).toEqual(blob);
  });

  it("drops invalid members and keeps the rest", () => {
    const good = mkSession("a");
    const out = sanitizePersisted({
      sessions: [
        good,
        null,
        "garbage",
        { id: "no-other-fields" },
        mkSession("bad-messages", { messages: [{ role: "user" }] as never }),
        mkSession("bad-role", { messages: [{ role: "alien", content: "x" }] as never }),
      ],
      currentSessionId: "a",
    });
    expect(out?.sessions).toEqual([good]);
  });

  it("returns null when no session survives (caller starts fresh)", () => {
    expect(sanitizePersisted({ sessions: [null, { id: "x" }] })).toBeNull();
    expect(sanitizePersisted({ sessions: [] })).toBeNull();
  });

  it("re-points currentSessionId at the first survivor when its target was dropped", () => {
    const out = sanitizePersisted({
      sessions: [mkSession("a"), "corrupt"],
      currentSessionId: "the-dropped-one",
    });
    expect(out?.currentSessionId).toBe("a");
  });

  it("accepts sessions with empty messages and optional message fields", () => {
    const s = mkSession("a", {
      messages: [{ role: "assistant", content: "", taskId: "t1", status: "done" }],
    });
    expect(sanitizePersisted({ sessions: [s], currentSessionId: "a" })?.sessions).toEqual([s]);
  });
});
