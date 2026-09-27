import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ADR 0114 D6 — after AppCrash's "Free up space & reload" clears the transcripts, nothing may
// write them straight back: not the crashed page's pagehide flush (the in-realm
// `__protoagentNoFlush` flag, set without importing this store), and not another tab's
// in-memory copy (the `storage-reset` broadcast).

type G = { __protoagentNoFlush?: boolean };
const KEY = "protoagent.chat.sessions";
const msg = (content: string) => [{ id: "m1", role: "assistant" as const, content }] as never[];

beforeEach(() => {
  window.localStorage.clear();
  vi.resetModules();
});

afterEach(() => {
  delete (globalThis as G).__protoagentNoFlush;
});

describe("chat-store stops persisting after a storage reset", () => {
  it("flushChatPersist writes nothing (and cancels the pending timer) once __protoagentNoFlush is set", async () => {
    vi.useFakeTimers();
    const { chatStore, flushChatPersist, PERSIST_DEBOUNCE_MS } = await import("./chat-store");
    const id = chatStore.getSnapshot().currentSessionId!;
    chatStore.updateMessages(id, msg("streamed")); // schedules the debounced write
    window.localStorage.removeItem(KEY); // AppCrash cleared it…
    (globalThis as G).__protoagentNoFlush = true; // …and set the flag
    flushChatPersist(); // pagehide
    vi.advanceTimersByTime(PERSIST_DEBOUNCE_MS * 2);
    expect(window.localStorage.getItem(KEY)).toBeNull();
    vi.useRealTimers();
  });

  it("without the flag the same flush does write (control)", async () => {
    vi.useFakeTimers();
    const { chatStore, flushChatPersist } = await import("./chat-store");
    const id = chatStore.getSnapshot().currentSessionId!;
    chatStore.updateMessages(id, msg("streamed"));
    flushChatPersist();
    expect(window.localStorage.getItem(KEY)).toContain("streamed");
    vi.useRealTimers();
  });

  it("a storage-reset broadcast from another tab stops this tab persisting", async () => {
    expect(typeof BroadcastChannel).toBe("function"); // Node provides it — no silent skip
    const { chatStore, flushChatPersist } = await import("./chat-store");
    const other = new BroadcastChannel("protoagent.storage");
    other.postMessage({ type: "storage-reset" });
    other.close();
    await new Promise((r) => setTimeout(r, 50)); // delivery is async
    window.localStorage.removeItem(KEY);
    const id = chatStore.getSnapshot().currentSessionId!;
    chatStore.renameSession(id, "after reset");
    chatStore.updateMessages(id, msg("after reset"));
    flushChatPersist();
    expect(window.localStorage.getItem(KEY)).toBeNull();
  });
});
