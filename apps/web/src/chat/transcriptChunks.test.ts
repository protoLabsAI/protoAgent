import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../lib/types";
import {
  CHUNK_CHARS,
  INLINE_MAX_CHARS,
  chunkHash,
  cutChunks,
  expandSessions,
  missingHistoryNote,
  orphanChunks,
  packSessions,
} from "./transcriptChunks";

// #4973: a save writes only what changed. Settled history lives in content-named chunks that
// are written once; the main key keeps each chat's unsettled tail.

const KEY = "protoagent.chat.sessions:agent-1";
const pad = (n: number) => "x".repeat(n);

/** `turns` prompt/answer pairs, each answer about `size` characters. */
function chat(turns: number, size: number, opts: { live?: boolean } = {}): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (let i = 0; i < turns; i++) {
    out.push({ id: `u${i}`, role: "user", content: `question ${i}`, status: "done" });
    out.push({ id: `a${i}`, role: "assistant", content: `${i}:${pad(size)}`, status: "done", taskId: `t${i}` });
  }
  if (opts.live) out[out.length - 1] = { ...out[out.length - 1], status: "streaming" };
  return out;
}

function storeOf(packed: ReturnType<typeof packSessions>) {
  return (key: string) => {
    const json = packed.chunkValues.get(key);
    return json ? (JSON.parse(json) as unknown[]) : null;
  };
}

describe("cutting a chat into chunks", () => {
  it("leaves a small chat fully inline", () => {
    const small = chat(3, 100);
    expect(cutChunks(small)).toEqual({ chunks: [], tail: small });
    expect(JSON.stringify(small).length).toBeLessThan(INLINE_MAX_CHARS);
  });

  it("closes chunks only on turn boundaries, and a settled chat keeps nothing inline", () => {
    const messages = chat(12, 9_000);
    const { chunks, tail } = cutChunks(messages);
    expect(tail).toEqual([]);
    expect(chunks.flat()).toEqual(messages);
    for (const chunk of chunks) expect(chunk[0].role).toBe("user");
    for (const chunk of chunks.slice(0, -1)) expect(JSON.stringify(chunk).length).toBeGreaterThanOrEqual(CHUNK_CHARS);
  });

  it("keeps the live turn and everything after it in the tail", () => {
    const messages = chat(12, 9_000, { live: true });
    const { chunks, tail } = cutChunks(messages);
    expect([...chunks.flat(), ...tail]).toEqual(messages);
    expect(tail[tail.length - 1].status).toBe("streaming");
    // A paused (parked) bubble is unsettled too.
    const parked = chat(12, 9_000);
    parked[5] = { ...parked[5], status: "streaming", paused: true };
    expect(cutChunks(parked).chunks.flat().length).toBeLessThanOrEqual(5);
  });

  it("appending a turn never changes an earlier chunk, so it's never rewritten", () => {
    const before = packSessions(KEY, [{ id: "s", messages: chat(12, 9_000) }]);
    const after = packSessions(KEY, [{ id: "s", messages: [...chat(12, 9_000), ...chat(1, 50).map((m) => ({ ...m, id: `n-${m.id}` }))] }]);
    const was = before.sessions[0].chunks!;
    const now = after.sessions[0].chunks!;
    // Every closed chunk survives by name; only the trailing group's chunk may change.
    expect(now.slice(0, was.length - 1)).toEqual(was.slice(0, -1));
  });

  it("names a chunk by its content", () => {
    expect(chunkHash("[1]")).toBe(chunkHash("[1]"));
    expect(chunkHash("[1]")).not.toBe(chunkHash("[2]"));
    expect(chunkHash("ab")).not.toBe(chunkHash("ba"));
  });
});

describe("packing and expanding the main key", () => {
  it("round-trips: chunks put back before the tail, `chunks` gone from the session", () => {
    const messages = chat(12, 9_000, { live: true });
    const sessions = [
      { id: "big", title: "Big", messages },
      { id: "small", title: "Small", messages: chat(1, 10) },
    ];
    const packed = packSessions(KEY, sessions);
    expect(packed.sessions[1]).toEqual(sessions[1]); // small: untouched, no chunks field
    const main = JSON.parse(JSON.stringify({ version: 1, sessions: packed.sessions }));
    // The main key carries only the big chat's tail: at most the open (unclosed) group of
    // turns plus the live one, whatever the chat's length.
    const liveTurn = JSON.stringify(messages.slice(-2)).length;
    expect(JSON.stringify(main.sessions[0].messages).length).toBeLessThan(CHUNK_CHARS + liveTurn);
    expect(JSON.stringify(main).length).toBeLessThan(JSON.stringify(messages).length / 2);
    expect(main.sessions[0].messages.at(-1).status).toBe("streaming");
    const expanded = expandSessions(KEY, main, storeOf(packed)) as { sessions: { messages: ChatMessage[]; chunks?: unknown }[] };
    expect(expanded.sessions[0].messages).toEqual(messages);
    expect(expanded.sessions[0].chunks).toBeUndefined();
    expect(expanded.sessions[1].messages).toEqual(sessions[1].messages);
  });

  it("a missing chunk becomes one note at the top, never a silent gap", () => {
    const packed = packSessions(KEY, [{ id: "big", messages: chat(12, 9_000) }]);
    const main = { sessions: packed.sessions };
    const first = [...packed.chunkValues.keys()][0];
    const read = (key: string) => (key === first ? null : storeOf(packed)(key));
    const expanded = expandSessions(KEY, main, read) as { sessions: { messages: ChatMessage[] }[] };
    expect(expanded.sessions[0].messages[0]).toEqual(missingHistoryNote("big"));
    expect(expanded.sessions[0].messages.filter((m) => m.id === missingHistoryNote("big").id)).toHaveLength(1);
  });

  it("an old inline value expands to itself", () => {
    const old = { version: 1, sessions: [{ id: "s", messages: chat(2, 10) }] };
    expect(expandSessions(KEY, old, () => null)).toEqual(old);
    expect(expandSessions(KEY, null, () => null)).toBeNull();
  });

  it("collects this agent's chunks that nothing names, and nobody else's", () => {
    const present = [
      `${KEY}.chunk.s.keep`,
      `${KEY}.chunk.s.stale`,
      `${KEY}.chunk.gone.old`,
      "protoagent.chat.sessions:other.chunk.s.x",
      `${KEY}.dismissed`,
      KEY,
    ];
    expect(orphanChunks(KEY, present, new Set([`${KEY}.chunk.s.keep`]))).toEqual([
      `${KEY}.chunk.s.stale`,
      `${KEY}.chunk.gone.old`,
    ]);
  });
});
