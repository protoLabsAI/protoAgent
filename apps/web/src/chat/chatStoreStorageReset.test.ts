import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ADR 0114 D6 — after AppCrash's "Free up space & reload" clears the transcripts, nothing may
// write them straight back: not the crashed page's pagehide flush (the in-realm
// `__protoagentNoFlush` flag, set without importing this store), and not another tab's
// in-memory copy (the `storage-reset` broadcast — which also reloads that tab).

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

  it("a storage-reset broadcast from another tab stops persisting AND reloads the tab", async () => {
    expect(typeof BroadcastChannel).toBe("function"); // Node provides it — no silent skip
    const reset = await import("../lib/storageReset");
    const reload = vi.fn();
    reset.__setStorageResetReloadForTests(reload);
    const { chatStore, flushChatPersist } = await import("./chat-store");
    const other = new BroadcastChannel("protoagent.storage");
    other.postMessage({ type: "storage-reset" });
    other.close();
    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1)); // delivery is async
    // Until the reload lands, nothing is written back.
    window.localStorage.removeItem(KEY);
    const id = chatStore.getSnapshot().currentSessionId!;
    chatStore.renameSession(id, "after reset");
    chatStore.updateMessages(id, msg("after reset"));
    flushChatPersist();
    expect(window.localStorage.getItem(KEY)).toBeNull();
  });

  it("the crash page that SENT the reset ignores its own broadcast (no reload out from under the key list)", async () => {
    const reset = await import("../lib/storageReset");
    const reload = vi.fn();
    reset.__setStorageResetReloadForTests(reload);
    (globalThis as G).__protoagentNoFlush = true;
    reset.handleStorageReset({ type: "storage-reset" });
    expect(reload).not.toHaveBeenCalled();
  });
});

describe("palette/DM threads honour the same block (review r1)", () => {
  it("neither an immediate save nor a pending trailing save writes after a reset", async () => {
    vi.useFakeTimers();
    const { savePaletteThread } = await import("../app/paletteChatStore");
    savePaletteThread({ contextId: "c1", messages: [] }, false); // trailing timer pending
    (globalThis as G).__protoagentNoFlush = true;
    vi.advanceTimersByTime(1000);
    savePaletteThread({ contextId: "c2", messages: [] }, true);
    savePaletteThread({ contextId: "c3", messages: [] }, false);
    vi.advanceTimersByTime(1000);
    expect(window.localStorage.getItem("protoagent.palette.chat")).toBeNull();
    vi.useRealTimers();
  });

  it("control: without the block the thread is saved", async () => {
    const { savePaletteThread } = await import("../app/paletteChatStore");
    savePaletteThread({ contextId: "c1", messages: [] }, true);
    expect(window.localStorage.getItem("protoagent.palette.chat")).toContain("c1");
  });
});
