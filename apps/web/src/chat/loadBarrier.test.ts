// ADR 0114 D2 — the load barrier (slice 4). Every session is read synchronously today,
// so these tests drive the barrier through its seam (`chatLoadBarrier.begin`) with an
// artificially asynchronous read: a session boots `pending`, and the test resolves or
// fails it later. The contract under test is the one S5's IndexedDB read relies on: a
// slow or failed read can NEVER overwrite history.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../lib/types";
import type { ChatSession, ChatState } from "./chat-store";

const KEY = "protoagent.chat.sessions";

const history: ChatMessage[] = [
  { id: "u1", role: "user", content: "What shipped?", status: "done" },
  { id: "a1", role: "assistant", content: "Slice 3.", status: "done" },
];

function stored(sessions: ChatSession[]) {
  window.localStorage.setItem(KEY, JSON.stringify({ version: 1, sessions, currentSessionId: sessions[0].id }));
}

function onDisk(id: string): ChatSession | undefined {
  const raw = window.localStorage.getItem(KEY);
  return raw ? (JSON.parse(raw).sessions as ChatSession[]).find((s) => s.id === id) : undefined;
}

function session(id: string, messages: ChatMessage[], extra: Partial<ChatSession> = {}): ChatSession {
  return { id, title: "Shipping", messages, createdAt: 1, updatedAt: 1, ...extra };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const card = (n: number): ChatMessage => ({
  id: `sched-${n}`,
  role: "system",
  content: `Scheduled task ${n} ran`,
  status: "done",
});

async function boot() {
  vi.resetModules();
  const store = await import("./chat-store");
  const liveness = await import("./sessionLiveness");
  const reattach = await import("./reattach");
  return { ...store, ...liveness, ...reattach };
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("updater-only message mutators", () => {
  it("applies updaters to the CURRENT transcript, in order", async () => {
    stored([session("s1", history)]);
    const { chatStore } = await boot();
    chatStore.updateMessages("s1", (messages) => [...messages, card(1)]);
    chatStore.updateMessages("s1", (messages) => [...messages, card(2)]);
    expect(chatStore.getSnapshot().sessions[0].messages.map((m) => m.id)).toEqual(["u1", "a1", "sched-1", "sched-2"]);
  });

  it("a missing target is a no-op: same snapshot, same session object, no updatedAt bump", async () => {
    stored([session("s1", history)]);
    const { chatStore, mapMessageById } = await boot();
    const before = chatStore.getSnapshot();
    chatStore.updateMessages("s1", (messages) => mapMessageById(messages, "gone", (m) => ({ ...m, content: "x" })));
    expect(chatStore.getSnapshot()).toBe(before);
    expect(chatStore.getSnapshot().sessions[0]).toBe(before.sessions[0]);
    expect(chatStore.getSnapshot().sessions[0].updatedAt).toBe(1);
  });

  it("an unknown session is ignored — the updater never runs", async () => {
    stored([session("s1", history)]);
    const { chatStore } = await boot();
    const updater = vi.fn((messages: ChatMessage[]) => [...messages, card(1)]);
    chatStore.updateMessages("nope", updater);
    expect(updater).not.toHaveBeenCalled();
  });

  it("mapMessageById hands back the input itself when the id is absent", async () => {
    const { mapMessageById } = await boot();
    expect(mapMessageById(history, "missing", (m) => m)).toBe(history);
    const next = mapMessageById(history, "a1", (m) => ({ ...m, content: "Slice 4." }));
    expect(next).not.toBe(history);
    expect(next[1].content).toBe("Slice 4.");
    expect(history[1].content).toBe("Slice 3."); // never mutated
  });
});

describe("a read that never resolves", () => {
  it("hydrate + scheduled card + send leave the persisted copy unchanged; the queue lands after the load", async () => {
    stored([session("s1", history), session("s2", [], { title: "New chat" })]);
    const { chatStore, chatLoadBarrier, flushChatPersist } = await boot();
    const read = deferred<ChatSession | null>();
    // The transcript is being read; its in-memory copy is a placeholder (S5: index only).
    chatStore.updateMessages("s1", () => []);
    flushChatPersist();
    stored([session("s1", history), session("s2", [], { title: "New chat" })]);
    chatLoadBarrier.begin(["s1"], () => read.promise);
    expect(chatStore.loadState("s1")).toBe("pending");

    // 1. Server hydration offers the session: an unread record is never "locally empty".
    expect(chatStore.captureHydrationEligibility("s1")).toBeNull();
    chatStore.hydrateSessions([
      session("s1", [{ id: "srv", role: "assistant", content: "from the server", status: "done" }], { updatedAt: 99 }),
    ]);
    // 2. A scheduled card arrives, 3. a send's bubbles are appended.
    chatStore.updateMessages("s1", (messages) => [...messages, card(1)]);
    chatStore.updateMessages("s1", (messages) => [
      ...messages,
      { id: "u2", role: "user", content: "And next?", status: "done" },
      { id: "a2", role: "assistant", content: "", status: "streaming", taskId: "t2" },
    ]);
    // Structural writes happen meanwhile (another session is created, renamed…).
    chatStore.createSession();
    chatStore.renameSession("s2", "Other");
    flushChatPersist();

    // The persisted copy is the record, untouched; the in-memory view shows nothing queued.
    expect(onDisk("s1")?.messages).toEqual(history);
    expect(chatStore.getSnapshot().sessions.find((s) => s.id === "s1")?.messages).toEqual([]);
    expect(chatLoadBarrier.queuedCount("s1")).toBe(2);

    // The read lands: the record, then every queued updater in order — the hydration
    // offer did NOT merge in.
    read.resolve(session("s1", history));
    await vi.waitFor(() => expect(chatStore.loadState("s1")).toBe("loaded"));
    const loaded = chatStore.getSnapshot().sessions.find((s) => s.id === "s1")!;
    expect(loaded.messages.map((m) => m.id)).toEqual(["u1", "a1", "sched-1", "u2", "a2"]);
    expect(chatLoadBarrier.queuedCount("s1")).toBe(0);
    // …and is persisted, with the live turn derived as streaming (S2).
    expect(onDisk("s1")?.messages.map((m) => m.id)).toEqual(["u1", "a1", "sched-1", "u2", "a2"]);
    expect(chatStore.getSnapshot().sessionStatusMap.s1).toBe("streaming");
  });

  it("never writes a pending session's in-memory copy, even when nothing is on disk", async () => {
    stored([session("s1", history), session("s2", [{ id: "x", role: "user", content: "hi", status: "done" }])]);
    const { chatStore, chatLoadBarrier, flushChatPersist } = await boot();
    chatLoadBarrier.begin(["s2"], () => new Promise(() => {}));
    window.localStorage.removeItem(KEY); // e.g. a tenant wipe raced the read
    chatStore.renameSession("s1", "Renamed");
    flushChatPersist();
    const written = JSON.parse(window.localStorage.getItem(KEY)!).sessions as ChatSession[];
    expect(written.map((s) => s.id)).toEqual(["s1"]);
  });
});

describe("timeout → failed → late resolve", () => {
  it("times out to failed, shows queued edits unsaved, then a late read loads and drains the queue", async () => {
    vi.useFakeTimers();
    stored([session("s1", history)]);
    const { chatStore, chatLoadBarrier, flushChatPersist, SESSION_LOAD_TIMEOUT_MS } = await boot();
    const read = deferred<ChatSession | null>();
    chatLoadBarrier.begin(["s1"], () => read.promise);

    vi.advanceTimersByTime(SESSION_LOAD_TIMEOUT_MS - 1);
    expect(chatStore.loadState("s1")).toBe("pending");
    vi.advanceTimersByTime(1);
    expect(chatStore.loadState("s1")).toBe("failed");

    // A failed session shows queued updaters in memory only — marked unsaved.
    chatStore.updateMessages("s1", (messages) => [...messages, card(7)]);
    const shown = chatStore.getSnapshot().sessions[0].messages;
    expect(shown[shown.length - 1].id).toBe("sched-7");
    expect(chatStore.hasUnsavedEdits("s1")).toBe(true);
    flushChatPersist();
    vi.runOnlyPendingTimers();
    expect(onDisk("s1")?.messages).toEqual(history);

    // The late read lands: failed → loaded, the queue replays onto the RECORD (not the
    // in-memory view it was already applied to — no double card).
    read.resolve(session("s1", [...history, { id: "a9", role: "assistant", content: "newer", status: "done" }]));
    await vi.waitFor(() => expect(chatStore.loadState("s1")).toBe("loaded"));
    expect(chatStore.getSnapshot().sessions[0].messages.map((m) => m.id)).toEqual(["u1", "a1", "a9", "sched-7"]);
    expect(chatStore.hasUnsavedEdits("s1")).toBe(false);
    expect(onDisk("s1")?.messages.map((m) => m.id)).toEqual(["u1", "a1", "a9", "sched-7"]);
  });

  it("a rejected read fails at once, and pageshow / visibilitychange retry it", async () => {
    stored([session("s1", history)]);
    const { chatStore, chatLoadBarrier } = await boot();
    const loader = vi
      .fn<(id: string) => Promise<ChatSession | null>>()
      .mockRejectedValueOnce(new Error("idb closed"))
      .mockRejectedValueOnce(new Error("still closed"))
      .mockResolvedValue(session("s1", history));
    chatLoadBarrier.begin(["s1"], loader);
    await vi.waitFor(() => expect(chatStore.loadState("s1")).toBe("failed"));

    window.dispatchEvent(new Event("pageshow"));
    await vi.waitFor(() => expect(loader).toHaveBeenCalledTimes(2));
    expect(chatStore.loadState("s1")).toBe("failed");

    document.dispatchEvent(new Event("visibilitychange"));
    await vi.waitFor(() => expect(chatStore.loadState("s1")).toBe("loaded"));
    expect(loader).toHaveBeenCalledTimes(3);
  });
});

describe("failed (and pending) are never read as empty", () => {
  it("unusedSession and the createSession reuse guard skip an unread blank", async () => {
    stored([session("blank", [], { title: "New chat" })]);
    const { chatStore, chatLoadBarrier, unusedSession, SESSION_LOAD_TIMEOUT_MS } = await boot();
    vi.useFakeTimers();
    chatLoadBarrier.begin(["blank"], () => new Promise(() => {}));
    expect(unusedSession(chatStore.getSnapshot())).toBeUndefined();
    vi.advanceTimersByTime(SESSION_LOAD_TIMEOUT_MS);
    expect(chatStore.loadState("blank")).toBe("failed");
    expect(unusedSession(chatStore.getSnapshot())).toBeUndefined();
    const created = chatStore.createSession();
    expect(created.id).not.toBe("blank"); // a new tab, not the unread one
    expect(chatStore.getSnapshot().sessions.map((s) => s.id)).toEqual(["blank", created.id]);
  });

  it("mergeHydratedSessions neither removes nor fills a failed placeholder", async () => {
    const { mergeHydratedSessions } = await boot();
    const blank = session("blank", [], { title: "New chat" });
    const base: ChatState = {
      version: 1,
      sessions: [blank],
      currentSessionId: "blank",
      activeSessions: ["blank"],
      sessionStatusMap: {},
      pendingDeleteRequest: null,
      pendingClearRequest: null,
      serverTurnControls: {},
      loadStateMap: { blank: "failed" },
    };
    const recovered = session("srv", [{ id: "r", role: "assistant", content: "hi", status: "done" }]);
    const merged = mergeHydratedSessions(base, [recovered, session("blank", recovered.messages)]);
    expect(merged.sessions.map((s) => s.id)).toEqual(["blank", "srv"]);
    expect(merged.sessions[0]).toBe(blank); // unread: untouched
    expect(merged.currentSessionId).toBe("blank");

    // The same state once LOADED is the ordinary boot placeholder, replaced as before.
    const loaded = mergeHydratedSessions({ ...base, loadStateMap: {} }, [recovered]);
    expect(loaded.sessions.map((s) => s.id)).toEqual(["srv"]);
  });

  it("durable hydration never fetches a session whose transcript hasn't loaded", async () => {
    stored([session("s1", [], { title: "New chat" })]);
    const { chatStore, chatLoadBarrier } = await boot();
    const { api } = await import("../lib/api");
    const { hydrateDurableChatSessions } = await import("./sessionHydration");
    chatLoadBarrier.begin(["s1"], () => new Promise(() => {}));
    vi.spyOn(api, "chatSessions").mockResolvedValue({
      sessions: [{ session_id: "s1", last_updated: "2026-09-27T00:00:00Z", turn_count: 1 }],
    } as Awaited<ReturnType<typeof api.chatSessions>>);
    const turns = vi.spyOn(api, "chatSessionTurns");
    await hydrateDurableChatSessions();
    expect(turns).not.toHaveBeenCalled();
    expect(chatStore.getSnapshot().sessions[0].messages).toEqual([]);
  });
});

describe("reconcile waits for the transcript", () => {
  it("never flips a pending session's streaming to idle; a failed one gets a status-only reconcile", async () => {
    vi.useFakeTimers();
    stored([session("s1", history)]);
    const {
      chatStore,
      chatLoadBarrier,
      reconcileSessionStatus,
      reconcileAllSessionStatuses,
      reattachOrReconcile,
      SESSION_LOAD_TIMEOUT_MS,
    } = await boot();
    chatStore.setSessionStatus("s1", "streaming");
    chatLoadBarrier.begin(["s1"], () => new Promise(() => {}));

    expect(reconcileSessionStatus("s1")).toBe(false);
    reconcileAllSessionStatuses();
    expect(reattachOrReconcile("s1")).toBeUndefined();
    expect(chatStore.getSnapshot().sessionStatusMap.s1).toBe("streaming");

    vi.advanceTimersByTime(SESSION_LOAD_TIMEOUT_MS);
    expect(chatStore.loadState("s1")).toBe("failed");
    const messages = chatStore.getSnapshot().sessions[0].messages;
    expect(reattachOrReconcile("s1")).toBeUndefined();
    expect(chatStore.getSnapshot().sessionStatusMap.s1).toBe("idle");
    expect(chatStore.getSnapshot().sessions[0].messages).toBe(messages); // status only
  });

  it("reconciles a session the moment its transcript loads", async () => {
    stored([session("s1", history)]);
    const { chatStore, chatLoadBarrier, watchSessionLiveness } = await boot();
    const stop = watchSessionLiveness();
    chatStore.setSessionStatus("s1", "streaming");
    const read = deferred<ChatSession | null>();
    chatLoadBarrier.begin(["s1"], () => read.promise);
    read.resolve(session("s1", history)); // a settled transcript: nothing live
    await vi.waitFor(() => expect(chatStore.getSnapshot().sessionStatusMap.s1).toBe("idle"));
    stop();
  });

  it("sessions created locally are born loaded", async () => {
    const { chatStore } = await boot();
    const created = chatStore.createSession({ incognito: true });
    expect(chatStore.loadState(created.id)).toBe("loaded");
  });
});
