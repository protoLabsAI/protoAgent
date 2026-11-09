import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";
import { openTranscriptStore, type SessionLike, type TranscriptStore } from "./transcriptStore";

// Epic #4981 S1: chat transcripts in IndexedDB, keeping #4984's content-named chunks so a
// streaming save writes only the tail record.

const pad = (n: number) => "x".repeat(n);
function longChat(turns: number, opts: { live?: boolean } = {}): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (let i = 0; i < turns; i++) {
    out.push({ id: `u${i}`, role: "user", content: `question ${i}`, status: "done" });
    out.push({ id: `a${i}`, role: "assistant", content: `${i}:${pad(9_000)}`, status: "done", taskId: `t${i}` });
  }
  if (opts.live) out[out.length - 1] = { ...out[out.length - 1], status: "streaming" };
  return out;
}
const session = (id: string, messages: ChatMessage[], extra: Record<string, unknown> = {}): SessionLike => ({
  id,
  title: `chat ${id}`,
  createdAt: 1,
  updatedAt: 10,
  messages,
  ...extra,
});

let factory: IDBFactory;
let store: TranscriptStore;

beforeEach(async () => {
  factory = new IDBFactory();
  store = (await openTranscriptStore({ factory }))!;
});
afterEach(() => store?.close());

async function rawCounts(): Promise<{ sessions: number; chunks: number }> {
  const db = await new Promise<IDBDatabase>((ok) => {
    const r = factory.open("protoagent-transcripts");
    r.onsuccess = () => ok(r.result);
  });
  const tx = db.transaction(["sessions", "chunks"]);
  const count = (name: string) =>
    new Promise<number>((ok) => {
      const r = tx.objectStore(name).count();
      r.onsuccess = () => ok(r.result);
    });
  const out = { sessions: await count("sessions"), chunks: await count("chunks") };
  db.close();
  return out;
}

describe("transcriptStore", () => {
  it("round-trips sessions per agent, whole and in order, with their metadata", async () => {
    const big = longChat(12);
    await store.putSessions("agent-a", [session("s1", big, { incognito: true }), session("s2", longChat(1), { updatedAt: 20 })]);
    await store.putSessions("agent-b", [session("s1", longChat(2))]);

    const a = (await store.loadAgent("agent-a")).sessions;
    expect(a.map((s) => s.id)).toEqual(["s2", "s1"]); // newest first
    expect(a[1].messages).toEqual(big);
    expect(a[1]).toMatchObject({ title: "chat s1", incognito: true, createdAt: 1 });
    expect((await store.loadAgent("agent-b")).sessions[0].messages).toHaveLength(4); // same id, other agent
    expect((await store.loadAgent("nobody")).sessions).toEqual([]);
    expect(await store.slugs()).toEqual(new Map([["agent-a", 2], ["agent-b", 1]]));
  });

  it("stores a long chat as chunks plus a small tail; a streaming save rewrites no chunk", async () => {
    const history = longChat(12);
    await store.putSessions("a", [session("s", history)]);
    const { chunks } = await rawCounts();
    expect(chunks).toBeGreaterThan(1);

    // A new turn streams: the settled chunks stay as they are, and none is written again.
    const put = vi.spyOn(IDBObjectStore.prototype, "put");
    for (let i = 1; i <= 3; i++) {
      await store.putSessions("a", [
        session("s", [...history, { id: "u-new", role: "user", content: "next", status: "done" }, { id: "a-new", role: "assistant", content: pad(i * 100), status: "streaming", taskId: "t-new" }]),
      ]);
    }
    const chunkPuts = put.mock.calls.filter(([value]) => (value as { hash?: string }).hash !== undefined);
    expect(chunkPuts).toEqual([]);
    expect(put.mock.calls.length).toBe(3); // one small session record per save
    put.mockRestore();
    const loaded = (await store.loadAgent("a")).sessions[0].messages;
    expect(loaded.map((m) => m.id)).toEqual([...history.map((m) => m.id), "u-new", "a-new"]);
    expect(loaded[loaded.length - 1].content).toBe(pad(300));
  });

  it("collects chunks an edit or a delete leaves unnamed", async () => {
    const history = longChat(12);
    await store.putSessions("a", [session("s", history)]);
    const before = (await rawCounts()).chunks;
    await store.putSessions("a", [session("s", history.map((m) => (m.id === "a0" ? { ...m, content: "edited" } : m)))]);
    expect((await rawCounts()).chunks).toBe(before); // one chunk replaced, none piling up
    expect((await store.loadAgent("a")).sessions[0].messages[1].content).toBe("edited");

    await store.deleteSessions("a", ["s"]);
    expect(await rawCounts()).toEqual({ sessions: 0, chunks: 0 });
  });

  it("keeps the saved tab order and focused chat", async () => {
    await store.putSessions("a", [session("s1", longChat(1), { updatedAt: 30 }), session("s2", longChat(1), { updatedAt: 10 }), session("s3", longChat(1), { updatedAt: 20 })]);
    await store.putAgent({ slug: "a", order: ["s2", "s1"], currentSessionId: "s1" });
    const { sessions, currentSessionId } = await store.loadAgent("a");
    // Named ids in saved order; one the order doesn't name follows.
    expect(sessions.map((s) => s.id)).toEqual(["s2", "s1", "s3"]);
    expect(currentSessionId).toBe("s1");
  });

  it("reloads chosen sessions for a cross-tab update", async () => {
    await store.putSessions("a", [session("s1", longChat(1)), session("s2", longChat(2))]);
    const got = await store.loadSessions("a", ["s2", "missing"]);
    expect(got.map((s) => [s.id, s.messages.length])).toEqual([["s2", 4]]);
  });

  it("a save re-puts chunks another tab deleted, so no message goes missing", async () => {
    const history = longChat(12);
    const other = (await openTranscriptStore({ factory }))!;
    await store.putSessions("a", [session("s", history)]);
    expect((await store.loadAgent("a")).sessions[0].messages).toEqual(history); // this tab has seen them
    await other.deleteSessions("a", ["s"]); // another tab's eviction takes the chunks
    await store.putSessions("a", [session("s", history)]); // this tab still has it open: re-save
    const reloaded = (await other.loadAgent("a")).sessions[0].messages;
    expect(reloaded).toEqual(history);
    expect(reloaded.some((m) => m.id?.startsWith("chunk-missing"))).toBe(false);
    other.close();
  });

  it("finds turns still streaming in other agents' chats", async () => {
    await store.putSessions("here", [session("s", longChat(1, { live: true }))]);
    await store.putSessions("other", [session("s", longChat(13, { live: true }), { title: "Long one" }), session("quiet", longChat(1))]);
    expect(await store.liveTurns("here")).toEqual([{ slug: "other", sessionId: "s", taskId: "t12", title: "Long one" }]);
  });

  it("clears everything (a backend switch)", async () => {
    await store.putSessions("a", [session("s", longChat(12))]);
    await store.clear();
    expect(await rawCounts()).toEqual({ sessions: 0, chunks: 0 });
  });

  it("a second tab's store sees what the first saved", async () => {
    const other = (await openTranscriptStore({ factory }))!;
    await store.putSessions("a", [session("s", longChat(12))]);
    expect((await other.loadAgent("a")).sessions[0].messages).toHaveLength(24);
    other.close();
  });
});

describe("eviction under quota pressure (ADR 0114 D5)", () => {
  const DAY = 24 * 60 * 60 * 1000;
  const NOW = 100 * DAY;
  it("frees space one step at a time, in order, never touching what's protected", async () => {
    await store.putSessions("here", [session("old", longChat(1), { updatedAt: NOW - 9 * DAY }), session("open", longChat(1), { updatedAt: NOW - 20 * DAY })]);
    await store.putSessions("gone", [session("g1", longChat(1), { updatedAt: NOW })]);
    await store.putSessions("quiet", [session("q1", longChat(1), { updatedAt: NOW - 3 * DAY })]);
    await store.putSessions("busy", [session("b1", longChat(1), { updatedAt: NOW - 60_000 })]);
    await store.putThread("protoagent.palette.chat:here", longChat(1));
    await store.putThread("protoagent.palette.chat:here:dm:x", longChat(1));
    await store.putThread("protoagent.palette.chat:quiet", longChat(1));
    const opts = { keepSlug: "here", fleet: new Set(["here", "quiet", "busy"]), protect: new Set(["open"]), threadPrefix: "protoagent.palette.chat:here", now: NOW };

    expect(await store.evictStep(opts)).toEqual([{ slug: "gone", id: "g1" }]); // 1. left the fleet
    expect(await store.evictStep(opts)).toEqual([{ slug: "palette", id: "protoagent.palette.chat:quiet" }]); // 2. other windows' threads
    expect(await store.evictStep(opts)).toEqual([{ slug: "quiet", id: "q1" }]); // 3. quiet for a day
    expect(await store.evictStep(opts)).toEqual([{ slug: "here", id: "old" }]); // 4. own oldest, not the open tab
    expect(await store.evictStep(opts)).toEqual([]); // nothing else may go
    expect((await store.loadAgent("busy")).sessions).toHaveLength(1);
    expect((await store.loadAgent("here")).sessions.map((s) => s.id)).toEqual(["open"]);
    expect([...(await store.loadThreads("protoagent.palette.chat:here")).keys()].sort()).toEqual([
      "protoagent.palette.chat:here",
      "protoagent.palette.chat:here:dm:x",
    ]);
  });
});

describe("opening the database", () => {
  it("is null when IndexedDB is missing or doesn't answer in time", async () => {
    const silent = { open: () => ({}) as IDBOpenDBRequest } as unknown as IDBFactory;
    expect(await openTranscriptStore({ factory: silent, timeoutMs: 20 })).toBeNull();
    const throwing = { open: () => { throw new Error("SecurityError"); } } as unknown as IDBFactory;
    expect(await openTranscriptStore({ factory: throwing })).toBeNull();
  });
});
