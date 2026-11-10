import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";

// Settings → Storage (docs/design/settings-storage.md): clearing one agent's chats from this
// browser, Free up space, and the `failing` signal for IndexedDB saves.

const pad = (n: number) => "x".repeat(n);
const chat = (turns: number, prefix = ""): ChatMessage[] =>
  Array.from({ length: turns }, (_, i) => [
    { id: `${prefix}u${i}`, role: "user" as const, content: `q${i}`, status: "done" as const },
    { id: `${prefix}a${i}`, role: "assistant" as const, content: `${i}:${pad(9_000)}`, status: "done" as const, taskId: `${prefix}t${i}` },
  ]).flat();

async function freshStore() {
  vi.resetModules();
  return import("./chat-store");
}

async function seed(slug: string, ids: string[], current?: string, at = 10) {
  const { openTranscriptStore } = await import("./transcriptStore");
  const store = (await openTranscriptStore())!;
  await store.putSessions(slug, ids.map((id, i) => ({ id, title: id, createdAt: 1, updatedAt: at + i, messages: chat(1, id) })));
  if (current) await store.putAgent({ slug, order: ids, currentSessionId: current });
  store.close();
}

async function savedIds(slug: string): Promise<string[]> {
  const { openTranscriptStore } = await import("./transcriptStore");
  const store = (await openTranscriptStore())!;
  const ids = (await store.loadAgent(slug)).sessions.map((s) => s.id);
  store.close();
  return ids.sort();
}

beforeEach(() => {
  (globalThis as { indexedDB: IDBFactory }).indexedDB = new IDBFactory();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("clearing one agent's saved chats (IndexedDB)", () => {
  it("removes another agent's chats from this browser and tells that agent's tabs", async () => {
    await seed("other", ["o1", "o2"]);
    await seed("host", ["h1"], "h1");
    const { initTranscriptBackend, clearSavedChats, TRANSCRIPT_CHANNEL } = await freshStore();
    await initTranscriptBackend();
    const heard: unknown[] = [];
    const channel = new BroadcastChannel(TRANSCRIPT_CHANNEL);
    channel.onmessage = (e) => heard.push(e.data);

    const res = await clearSavedChats("other");
    expect(res.chats).toBe(2);
    expect(res.bytes).toBeGreaterThan(2 * 9_000);
    expect(await savedIds("other")).toEqual([]);
    expect(await savedIds("host")).toEqual(["h1"]);
    await vi.waitFor(() => expect(heard).toEqual([{ agent: "other", ids: [], deleted: [], evicted: ["o1", "o2"] }]));
    channel.close();
  });

  it("this window's agent keeps the chats open in a tab and drops the rest, here too", async () => {
    await seed("host", ["open", "old1", "old2"], "open");
    const { chatStore, initTranscriptBackend, clearSavedChats, transcriptWritesSettled } = await freshStore();
    await initTranscriptBackend();
    expect(chatStore.getSnapshot().activeSessions).toEqual(["open"]);

    const res = await clearSavedChats("host");
    expect(res.chats).toBe(2);
    expect(chatStore.getSnapshot().sessions.map((s) => s.id)).toEqual(["open"]);
    await transcriptWritesSettled();
    expect(await savedIds("host")).toEqual(["open"]);
  });

  it("is not an automatic clear: no eviction is reported and no record is written", async () => {
    await seed("other", ["o1"]);
    const { initTranscriptBackend, clearSavedChats } = await freshStore();
    const { subscribeEvictions, lastAutoClear } = await import("../lib/storageEviction");
    await initTranscriptBackend();
    const heard = vi.fn();
    const off = subscribeEvictions(heard);
    await clearSavedChats("other");
    off();
    expect(heard).not.toHaveBeenCalled();
    expect(lastAutoClear()).toBeNull();
  });
});

describe("clearing one agent's saved chats (localStorage fallback)", () => {
  const MAIN = "protoagent.chat.sessions";
  const v1 = (ids: string[], current: string) =>
    JSON.stringify({
      version: 1,
      sessions: ids.map((id, i) => ({ id, title: id, createdAt: 1, updatedAt: 5 + i, messages: chat(1, id) })),
      currentSessionId: current,
    });

  it("removes another agent's key and its chunks", async () => {
    localStorage.setItem(`${MAIN}:other`, v1(["o1", "o2"], "o1"));
    localStorage.setItem(`${MAIN}:other.chunk.o1.abc`, "[]");
    localStorage.setItem(`${MAIN}:other.dismissed`, "[]");
    const { initTranscriptBackend, clearSavedChats } = await freshStore();
    expect(await initTranscriptBackend({ open: async () => null })).toBe("local");

    const res = await clearSavedChats("other");
    expect(res.chats).toBe(2);
    expect(res.bytes).toBeGreaterThan(0);
    expect(localStorage.getItem(`${MAIN}:other`)).toBeNull();
    expect(localStorage.getItem(`${MAIN}:other.chunk.o1.abc`)).toBeNull();
    // The dismissal set isn't a chat.
    expect(localStorage.getItem(`${MAIN}:other.dismissed`)).toBe("[]");
  });

  it("this window's agent keeps its open chat and rewrites its key without the others", async () => {
    localStorage.setItem(MAIN, v1(["open", "old"], "open"));
    const { chatStore, initTranscriptBackend, clearSavedChats } = await freshStore();
    await initTranscriptBackend({ open: async () => null });
    expect(chatStore.getSnapshot().activeSessions).toEqual(["open"]);

    const res = await clearSavedChats("host");
    expect(res.chats).toBe(1);
    expect(chatStore.getSnapshot().sessions.map((s) => s.id)).toEqual(["open"]);
    expect(JSON.parse(localStorage.getItem(MAIN)!).sessions.map((s: { id: string }) => s.id)).toEqual(["open"]);
  });
});

describe("saving to IndexedDB fails, then Free up space", () => {
  it("a save that still fails after clearing everything it may sets `failing`", async () => {
    const { chatStore, initTranscriptBackend, flushChatPersist, transcriptWritesSettled, transcriptSaveFailing } = await freshStore();
    const { TranscriptStore } = await import("./transcriptStore");
    await initTranscriptBackend();
    const put = vi.spyOn(TranscriptStore.prototype, "putSessions").mockRejectedValue(new DOMException("full", "QuotaExceededError"));
    const id = chatStore.getSnapshot().currentSessionId!;
    chatStore.updateMessages(id, chat(1));
    flushChatPersist();
    await transcriptWritesSettled();
    expect(transcriptSaveFailing()).toBe(true);
    put.mockRestore();
  });

  it("clears old chats in the automatic order until the save fits, and records it as an automatic clear", async () => {
    // Written just now, so only "left the fleet" can clear them.
    await seed("gone-agent", ["g1", "g2"], undefined, Date.now());
    const { chatStore, initTranscriptBackend, flushChatPersist, transcriptWritesSettled, transcriptSaveFailing, freeUpSpace } =
      await freshStore();
    const { TranscriptStore } = await import("./transcriptStore");
    const { setFleetSlugs, subscribeEvictions, lastAutoClear } = await import("../lib/storageEviction");
    await initTranscriptBackend();

    // Full: every save fails until something is cleared.
    let full = true;
    const real = TranscriptStore.prototype.putSessions;
    const put = vi.spyOn(TranscriptStore.prototype, "putSessions").mockImplementation(async function (
      this: InstanceType<typeof TranscriptStore>,
      ...args
    ) {
      if (full) throw new DOMException("full", "QuotaExceededError");
      return real.apply(this, args);
    });
    const id = chatStore.getSnapshot().currentSessionId!;
    setFleetSlugs(null); // unknown fleet: the first squeeze can't clear the departed agent
    chatStore.updateMessages(id, chat(1));
    flushChatPersist();
    await transcriptWritesSettled();
    expect(transcriptSaveFailing()).toBe(true);
    expect(await savedIds("gone-agent")).toEqual(["g1", "g2"]);

    // The fleet list arrives; Free up space now clears the departed agent, then the save fits.
    setFleetSlugs(["host"]);
    const toasts = vi.fn();
    const off = subscribeEvictions(toasts);
    put.mockImplementation(async function (this: InstanceType<typeof TranscriptStore>, ...args) {
      if (full) {
        full = false; // the first failed retry triggers a step; after it, there's room
        throw new DOMException("full", "QuotaExceededError");
      }
      return real.apply(this, args);
    });
    const res = await freeUpSpace();
    off();
    put.mockRestore();
    setFleetSlugs(null);

    expect(res.chats).toBe(2);
    expect(res.bytes).toBeGreaterThan(2 * 9_000);
    expect(res.saving).toBe(true);
    expect(transcriptSaveFailing()).toBe(false);
    expect(await savedIds("gone-agent")).toEqual([]);
    expect(await savedIds("host")).toContain(id);
    // Reported as `free`, so StorageUpkeep doesn't toast it a second time.
    expect(toasts.mock.calls.map((c) => c[0].reason)).toEqual(["free"]);
    const record = lastAutoClear()!;
    expect(record.chats).toBe(2);
    expect(record.agents).toEqual([{ slug: "gone-agent", chats: 2, gone: true }]);
    expect(record.bytes).toBe(res.bytes);
  });
});
