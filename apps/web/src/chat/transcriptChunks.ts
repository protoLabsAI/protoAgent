// Chunked transcript storage (#4973): a save writes only what changed.
//
// A chat's transcript used to live inline in one localStorage value per agent, so every save
// re-wrote the agent's WHOLE history. WebKit stores that value as one SQLite row, and an edit
// shifts every overflow page after it, so a 271K-character chat cost ~540 KB per save,
// ~120 times a streamed minute. That piled up into a 250 MB write-ahead log in the desktop app
// (#4974).
//
// Now a chat's SETTLED history is cut into chunks, each its own key named by a hash of its
// content, so a chunk is written once and never rewritten (a changed chunk is a new key; the
// old one is collected). The agent's main key keeps each chat's metadata, the chunk list, and
// only the unsettled tail. While a reply streams, a save rewrites just that tail.
//
//   protoagent.chat.sessions[:<slug>]                           main key: { sessions: [{…, chunks, messages: tail}] }
//   protoagent.chat.sessions[:<slug>].chunk.<sessionId>.<hash>  a JSON array of settled messages
//
// Small chats stay fully inline (INLINE_MAX_CHARS), so a short chat reads exactly as before.
// The main key's tail always holds the live turn, so FleetTurnWatch, which reads other agents'
// main keys for a streaming last bubble, works unchanged.
//
// Chunk boundaries are deterministic from the start of the chat: a chunk closes once it holds
// CHUNK_CHARS and the next message opens a turn (role "user"). So appending to a chat never
// moves an earlier boundary, and earlier chunks keep their hashes. The trailing group closes
// only once nothing in the chat is still streaming or paused.

import type { ChatMessage } from "../lib/types";

/** A chat whose serialized messages are under this stays fully inline in the main key. */
export const INLINE_MAX_CHARS = 16_000;
/** A chunk closes at the first turn boundary once it holds this many characters. */
export const CHUNK_CHARS = 32_000;

type Session = { id: string; messages: ChatMessage[]; chunks?: string[] };

/** The key prefix holding one chat's chunks. */
export function chunkPrefix(storageKey: string, sessionId: string): string {
  return `${storageKey}.chunk.${sessionId}.`;
}

/** Every chunk key of one agent's main key starts with this. */
export function agentChunkPrefix(storageKey: string): string {
  return `${storageKey}.chunk.`;
}

function fnv1a(text: string, seed: number): number {
  let h = seed >>> 0;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

/** A content name for a chunk: its length and two independent 32-bit FNV-1a hashes. */
export function chunkHash(json: string): string {
  return `${json.length.toString(36)}-${fnv1a(json, 0x811c9dc5).toString(36)}-${fnv1a(json, 0x01000193).toString(36)}`;
}

function unsettled(message: ChatMessage): boolean {
  return message.status === "streaming" || Boolean(message.paused);
}

/** Split one chat into settled chunks plus the inline tail. Pure: same messages → same cut. */
export function cutChunks(messages: ChatMessage[]): { chunks: ChatMessage[][]; tail: ChatMessage[] } {
  const total = messages.reduce((n, m) => n + JSON.stringify(m).length, 0);
  if (total < INLINE_MAX_CHARS) return { chunks: [], tail: messages };
  // Nothing at or after the first unsettled message is chunked: its turn may still change.
  const firstLive = messages.findIndex(unsettled);
  const settledEnd = firstLive === -1 ? messages.length : firstLive;
  const chunks: ChatMessage[][] = [];
  let start = 0;
  let size = 0;
  for (let i = 0; i < settledEnd; i++) {
    size += JSON.stringify(messages[i]).length;
    const next = messages[i + 1];
    const boundary = !next || next.role === "user";
    if (size >= CHUNK_CHARS && boundary && i + 1 <= settledEnd) {
      chunks.push(messages.slice(start, i + 1));
      start = i + 1;
      size = 0;
    }
  }
  // A fully settled chat chunks its trailing group too: nothing of it stays in the main key.
  if (firstLive === -1 && start < messages.length) {
    chunks.push(messages.slice(start));
    start = messages.length;
  }
  return { chunks, tail: messages.slice(start) };
}

export type PackedSessions<S> = {
  /** Sessions as the main key stores them: `chunks` names, `messages` the tail. */
  sessions: (S & { chunks?: string[] })[];
  /** Chunk keys this pack references, each with its JSON. Unchanged chunks are listed too;
   *  the writer skips the ones already present. */
  chunkValues: Map<string, string>;
};

/** Pack sessions for the main key. A session too small to chunk is returned as it is. */
export function packSessions<S extends Session>(storageKey: string, sessions: S[]): PackedSessions<S> {
  const chunkValues = new Map<string, string>();
  const packed = sessions.map((session) => {
    const { chunks, tail } = cutChunks(session.messages);
    const { chunks: _stale, ...rest } = session;
    void _stale;
    if (!chunks.length) return rest as S;
    const names = chunks.map((chunk) => {
      const json = JSON.stringify(chunk);
      const name = chunkHash(json);
      chunkValues.set(chunkPrefix(storageKey, session.id) + name, json);
      return name;
    });
    return { ...rest, chunks: names, messages: tail } as S & { chunks: string[] };
  });
  return { sessions: packed, chunkValues };
}

/** The note that stands in for history a chat's chunk no longer has in this browser. */
export function missingHistoryNote(sessionId: string): ChatMessage {
  return {
    id: `chunk-missing-${sessionId}`,
    role: "system",
    content: "Earlier messages in this chat couldn't be loaded from this browser.",
    status: "done",
  } as ChatMessage;
}

/** Expand a parsed main-key value: each session's chunks, read through `read`, then its tail.
 *  A missing or corrupt chunk becomes one note at the top, not a silent gap. Anything that
 *  isn't a session list is returned untouched (sanitizePersisted judges it). */
export function expandSessions(storageKey: string, parsed: unknown, read: (key: string) => unknown[] | null): unknown {
  if (!parsed || typeof parsed !== "object") return parsed;
  const p = parsed as { sessions?: unknown };
  if (!Array.isArray(p.sessions)) return parsed;
  const sessions = p.sessions.map((raw) => {
    if (!raw || typeof raw !== "object") return raw;
    const session = raw as { id?: unknown; chunks?: unknown; messages?: unknown };
    if (!Array.isArray(session.chunks)) return raw;
    const { chunks, ...rest } = session as Record<string, unknown> & { chunks: unknown[] };
    const id = typeof session.id === "string" ? session.id : "";
    const earlier: unknown[] = [];
    let missing = false;
    for (const name of chunks) {
      const messages = typeof name === "string" && id ? read(chunkPrefix(storageKey, id) + name) : null;
      if (messages) earlier.push(...messages);
      else missing = true;
    }
    const tail = Array.isArray(session.messages) ? session.messages : [];
    return { ...rest, messages: [...(missing ? [missingHistoryNote(id)] : []), ...earlier, ...tail] };
  });
  return { ...p, sessions };
}

/** Chunk keys of this agent that `referenced` doesn't name: safe to remove once the main key
 *  that no longer names them is written. */
export function orphanChunks(storageKey: string, present: Iterable<string>, referenced: ReadonlySet<string>): string[] {
  const prefix = agentChunkPrefix(storageKey);
  const out: string[] = [];
  for (const key of present) if (key.startsWith(prefix) && !referenced.has(key)) out.push(key);
  return out;
}
