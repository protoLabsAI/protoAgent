import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";

// #4973 end to end through the store: a long chat's settled history is written once into
// chunk keys; while a reply streams, a save rewrites only the main key's small tail; reload
// puts the chat back whole; deleting the chat collects its chunks.

const MAIN = "protoagent.chat.sessions";
const pad = (n: number) => "x".repeat(n);

function longChat(turns: number): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (let i = 0; i < turns; i++) {
    out.push({ id: `u${i}`, role: "user", content: `question ${i}`, status: "done" });
    out.push({ id: `a${i}`, role: "assistant", content: `${i}:${pad(9_000)}`, status: "done", taskId: `t${i}` });
  }
  return out;
}

const chunkKeys = () => Object.keys(localStorage).filter((k) => k.startsWith(`${MAIN}.chunk.`));

describe("a long chat saves in chunks", () => {
  let setItem: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    window.localStorage.clear();
    window.history.replaceState(null, "", "/");
    vi.resetModules();
    setItem = vi.spyOn(Storage.prototype, "setItem");
  });

  afterEach(() => {
    setItem.mockRestore();
  });

  it("writes settled history once, rewrites only the tail while streaming, and reloads whole", async () => {
    const { chatStore, flushChatPersist } = await import("./chat-store");
    const id = chatStore.getSnapshot().currentSessionId!;
    const history = longChat(12);
    chatStore.updateMessages(id, history);
    flushChatPersist();

    const chunks = chunkKeys();
    expect(chunks.length).toBeGreaterThan(1);
    const main = localStorage.getItem(MAIN)!;
    expect(main.length).toBeLessThan(JSON.stringify(history).length / 4); // all settled: nothing inline

    // A new turn streams: several saves, and not one chunk write.
    setItem.mockClear();
    const prompt: ChatMessage = { id: "u-new", role: "user", content: "and now?", status: "done" };
    for (let i = 1; i <= 5; i++) {
      chatStore.updateMessages(id, [...history, prompt, { id: "a-new", role: "assistant", content: pad(i * 200), status: "streaming", taskId: "t-new" }]);
      flushChatPersist();
    }
    const written = setItem.mock.calls.map((call: unknown[]) => call[0] as string);
    expect(written.filter((key: string) => key.startsWith(`${MAIN}.chunk.`))).toEqual([]);
    expect(written.filter((key: string) => key === MAIN).length).toBe(5);
    // The main key holds the live bubble, so another window's FleetTurnWatch still sees it.
    const tail = JSON.parse(localStorage.getItem(MAIN)!).sessions.find((s: { id: string }) => s.id === id).messages;
    expect(tail.at(-1)).toMatchObject({ id: "a-new", status: "streaming" });

    // Reload: the chat comes back whole, in order.
    vi.resetModules();
    const reloaded = await import("./chat-store");
    const messages = reloaded.chatStore.getSnapshot().sessions.find((s) => s.id === id)!.messages;
    expect(messages.map((m) => m.id)).toEqual([...history.map((m) => m.id), "u-new", "a-new"]);
    expect(messages[1].content).toBe(history[1].content);
  });

  it("an edited earlier turn gets a new chunk, and the old one is collected", async () => {
    const { chatStore, flushChatPersist } = await import("./chat-store");
    const id = chatStore.getSnapshot().currentSessionId!;
    const history = longChat(12);
    chatStore.updateMessages(id, history);
    flushChatPersist();
    const before = new Set(chunkKeys());

    chatStore.updateMessages(id, history.map((m) => (m.id === "a0" ? { ...m, content: "rewritten" } : m)));
    flushChatPersist();
    const after = new Set(chunkKeys());
    expect([...before].filter((k) => !after.has(k)).length).toBeGreaterThan(0); // the old first chunk is gone
    expect([...after].filter((k) => !before.has(k)).length).toBeGreaterThan(0); // and a new one replaced it

    vi.resetModules();
    const reloaded = await import("./chat-store");
    expect(reloaded.chatStore.getSnapshot().sessions.find((s) => s.id === id)!.messages[1].content).toBe("rewritten");
  });

  it("deleting the chat collects its chunks", async () => {
    const { chatStore, flushChatPersist } = await import("./chat-store");
    const id = chatStore.getSnapshot().currentSessionId!;
    chatStore.updateMessages(id, longChat(12));
    flushChatPersist();
    expect(chunkKeys().length).toBeGreaterThan(0);
    chatStore.createSession();
    chatStore.deleteSession(id);
    flushChatPersist();
    expect(chunkKeys()).toEqual([]);
  });
});
