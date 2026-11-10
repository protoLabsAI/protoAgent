import { beforeEach, describe, expect, it } from "vitest";

import { readStorageUsage } from "./storageUsage";

// Settings ▸ Storage on the localStorage fallback (the transcript store didn't open): the rows
// come from the key registry, and every other key counts as layout and preferences.

const chats = (ids: string[], updatedAt: number) =>
  JSON.stringify({ version: 1, sessions: ids.map((id) => ({ id, updatedAt, messages: [{ id: `${id}-m`, content: "x".repeat(500) }] })) });

beforeEach(() => localStorage.clear());

describe("readStorageUsage without the transcript store", () => {
  it("groups transcript keys and their chunks by agent, threads as quick chats, the rest as preferences", async () => {
    localStorage.setItem("protoagent.chat.sessions", chats(["open", "old"], 50));
    localStorage.setItem("protoagent.chat.sessions:ava", chats(["a1"], 70));
    localStorage.setItem("protoagent.chat.sessions:ava.chunk.a1.abc", "x".repeat(2000));
    localStorage.setItem("protoagent.chat.sessions:ava.dismissed", "[]");
    localStorage.setItem("protoagent.palette.chat:ava", JSON.stringify({ contextId: "c", messages: [{ id: "p" }] }));
    localStorage.setItem("protoagent.palette.chat", JSON.stringify({ contextId: "c2", messages: [] }));
    localStorage.setItem("protoagent.authToken", "secret");
    localStorage.setItem("some-plugin.key", "y".repeat(100));

    const usage = await readStorageUsage({ store: null, here: "host", protect: new Set(["open"]) });
    expect(usage.backend).toBe("local");
    expect(usage.agents.map((a) => [a.slug, a.chats, a.lastSavedAt])).toEqual([
      ["ava", 1, 70],
      ["host", 2, 50],
    ]);
    const ava = usage.agents[0];
    expect(ava.bytes).toBeGreaterThan(2 * 2000); // the chunk counts toward its agent
    expect(ava.clearable).toEqual({ chats: 1, bytes: ava.bytes });
    // This window's agent: Clear keeps the open chat.
    expect(usage.agents[1].clearable.chats).toBe(1);
    expect(usage.agents[1].clearable.bytes).toBeLessThan(usage.agents[1].bytes);
    // One thread with messages; an empty one is a key, not a chat.
    expect(usage.quick.threads).toBe(1);
    expect(usage.quick.bytes).toBeGreaterThan(0);
    // Dismissals, sign-in and an unregistered plugin key.
    expect(usage.prefsBytes).toBe(
      2 * ("protoagent.chat.sessions:ava.dismissed".length + 2) + 2 * ("protoagent.authToken".length + 6) + 2 * ("some-plugin.key".length + 100),
    );
  });
});
