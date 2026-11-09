import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";

// Epic #4981 S2: the chat store on IndexedDB behind a boot gate. localStorage transcripts are
// migrated once, saves write only the chats that changed, other tabs hear about them, and a
// store that fails or comes back late leaves the page on localStorage.

const MAIN = "protoagent.chat.sessions";
const pad = (n: number) => "x".repeat(n);
const chat = (turns: number, prefix = ""): ChatMessage[] =>
  Array.from({ length: turns }, (_, i) => [
    { id: `${prefix}u${i}`, role: "user" as const, content: `q${i}`, status: "done" as const },
    { id: `${prefix}a${i}`, role: "assistant" as const, content: `${i}:${pad(9_000)}`, status: "done" as const, taskId: `${prefix}t${i}` },
  ]).flat();
const v1 = (sessions: { id: string; messages: ChatMessage[]; updatedAt?: number }[], current?: string) =>
  JSON.stringify({
    version: 1,
    sessions: sessions.map((s) => ({ title: `chat ${s.id}`, createdAt: 1, updatedAt: s.updatedAt ?? 5, ...s })),
    currentSessionId: current ?? sessions[0]?.id,
  });

async function freshStore() {
  vi.resetModules();
  return import("./chat-store");
}

async function reopen() {
  const { openTranscriptStore } = await import("./transcriptStore");
  return (await openTranscriptStore())!;
}

beforeEach(() => {
  (globalThis as { indexedDB: IDBFactory }).indexedDB = new IDBFactory();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
});
afterEach(async () => {
  vi.useRealTimers();
});

describe("booting onto IndexedDB", () => {
  it("migrates every agent's localStorage transcripts once, then loads this agent's", async () => {
    localStorage.setItem(MAIN, v1([{ id: "h1", messages: chat(12) }, { id: "h2", messages: chat(1) }], "h2"));
    localStorage.setItem(`${MAIN}:other-1`, v1([{ id: "o1", messages: chat(2) }]));
    localStorage.setItem(`${MAIN}.dismissed`, "[]");
    const { chatStore, initTranscriptBackend } = await freshStore();

    expect(await initTranscriptBackend()).toBe("idb");
    const snap = chatStore.getSnapshot();
    expect(snap.sessions.map((s) => s.id).sort()).toEqual(["h1", "h2"]);
    expect(snap.currentSessionId).toBe("h2");
    expect(snap.sessions.find((s) => s.id === "h1")!.messages).toHaveLength(24);
    // The transcript keys are gone from localStorage; the dismissal set stays.
    expect(localStorage.getItem(MAIN)).toBeNull();
    expect(localStorage.getItem(`${MAIN}:other-1`)).toBeNull();
    expect(localStorage.getItem(`${MAIN}.dismissed`)).toBe("[]");
    // The other agent's chats were imported too.
    const store = await reopen();
    expect((await store.loadAgent("other-1")).sessions.map((s) => s.id)).toEqual(["o1"]);
    store.close();
  });

  it("a migration never overwrites a newer saved copy", async () => {
    const { openTranscriptStore } = await import("./transcriptStore");
    const store = (await openTranscriptStore())!;
    await store.putSessions("host", [{ id: "s", title: "newer", createdAt: 1, updatedAt: 50, messages: chat(1, "n") }]);
    store.close();
    localStorage.setItem(MAIN, v1([{ id: "s", messages: chat(1, "old"), updatedAt: 5 }]));
    const { chatStore, initTranscriptBackend } = await freshStore();
    await initTranscriptBackend();
    expect(chatStore.getSnapshot().sessions[0].messages[0].id).toBe("nu0");
  });

  it("saves only the chats that changed, and a reload brings them back", async () => {
    const { chatStore, initTranscriptBackend, flushChatPersist, transcriptWritesSettled } = await freshStore();
    await initTranscriptBackend();
    const a = chatStore.getSnapshot().currentSessionId!;
    chatStore.updateMessages(a, chat(12));
    const b = chatStore.createSession().id;
    chatStore.updateMessages(b, chat(1, "b"));
    flushChatPersist();
    await transcriptWritesSettled();

    const put = vi.spyOn(IDBObjectStore.prototype, "put");
    chatStore.updateMessages(b, [...chat(1, "b"), { id: "x", role: "user", content: "more", status: "done" }]);
    flushChatPersist();
    await transcriptWritesSettled();
    const records = put.mock.calls.map(([v]) => v as { id?: string; hash?: string });
    expect(records.filter((r) => r.hash === undefined).map((r) => r.id)).toEqual([b]); // chat a untouched
    put.mockRestore();
    expect(localStorage.getItem(MAIN)).toBeNull(); // nothing transcript-sized in localStorage

    const reloaded = await freshStore();
    await reloaded.initTranscriptBackend();
    const snap = reloaded.chatStore.getSnapshot();
    expect(snap.sessions.find((s) => s.id === a)!.messages).toHaveLength(24);
    expect(snap.sessions.find((s) => s.id === b)!.messages.map((m) => m.id)).toEqual(["bu0", "ba0", "x"]);
    expect(snap.currentSessionId).toBe(b);
  });

  it("deleting a chat removes it from the store", async () => {
    const { chatStore, initTranscriptBackend, flushChatPersist, transcriptWritesSettled } = await freshStore();
    await initTranscriptBackend();
    const a = chatStore.getSnapshot().currentSessionId!;
    chatStore.updateMessages(a, chat(12));
    flushChatPersist();
    await transcriptWritesSettled();
    chatStore.createSession();
    chatStore.deleteSession(a);
    flushChatPersist();
    await transcriptWritesSettled();
    const store = await reopen();
    expect((await store.loadAgent("host")).sessions.map((s) => s.id)).not.toContain(a);
    store.close();
  });

  it("another tab's save reaches this one over the channel", async () => {
    const first = await freshStore();
    await first.initTranscriptBackend();
    const id = first.chatStore.getSnapshot().currentSessionId!;
    first.chatStore.updateMessages(id, chat(1));
    first.flushChatPersist();
    await first.transcriptWritesSettled();

    // A second tab (a fresh module = its own state, store connection and channel).
    const second = await freshStore();
    await second.initTranscriptBackend();
    second.chatStore.updateMessages(id, [...chat(1), { id: "from-2", role: "user", content: "hi", status: "done" }]);
    second.flushChatPersist();
    await second.transcriptWritesSettled();

    await vi.waitFor(() => {
      expect(first.chatStore.getSnapshot().sessions.find((s) => s.id === id)!.messages.map((m) => m.id)).toContain("from-2");
    });
  });
});

describe("staying on localStorage", () => {
  it("when IndexedDB isn't available", async () => {
    localStorage.setItem(MAIN, v1([{ id: "h1", messages: chat(1) }]));
    const { chatStore, initTranscriptBackend, flushChatPersist } = await freshStore();
    expect(await initTranscriptBackend({ open: async () => null })).toBe("local");
    chatStore.updateMessages("h1", [...chat(1), { id: "n", role: "user", content: "new", status: "done" }]);
    flushChatPersist();
    expect(JSON.parse(localStorage.getItem(MAIN)!).sessions[0].messages.map((m: ChatMessage) => m.id)).toContain("n");
  });

  it("when the store answers after the boot deadline: the page never swaps state under itself", async () => {
    localStorage.setItem(MAIN, v1([{ id: "h1", messages: chat(1) }]));
    const { chatStore, initTranscriptBackend } = await freshStore();
    const { openTranscriptStore } = await import("./transcriptStore");
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const slowOpen = async () => {
      await gate;
      return openTranscriptStore();
    };
    expect(await initTranscriptBackend({ open: slowOpen, deadlineMs: 20 })).toBe("local");
    const before = chatStore.getSnapshot();
    release();
    await new Promise((r) => setTimeout(r, 50));
    expect(chatStore.getSnapshot()).toBe(before);
    expect(localStorage.getItem(MAIN)).not.toBeNull(); // still the localStorage page
  });
});
