// Chat transcripts in IndexedDB (ADR 0114 D2, epic #4981 S1).
//
// Why IndexedDB: WebKit's localStorage leaves a SQLite read transaction open the first time a
// value over 1K characters is overwritten, and from then on its write-ahead log never
// checkpoints. Every save of a transcript was such an overwrite, so the desktop app's log grew
// to 300+ MB a day (#4974). WebKit's IndexedDB backend checkpoints normally under the same load,
// and gets far more quota than one origin's ~5 MB of localStorage.
//
// The layout keeps #4984's content-named chunks, so a save during a streaming reply still writes
// only the small tail record:
//
//   sessions  key [slug, id]               { slug, id, updatedAt, session: <ChatSession minus messages>,
//                                            chunks: [hash…], tail: ChatMessage[] }
//   chunks    key [slug, sessionId, hash]  { slug, sessionId, hash, messages: ChatMessage[] }
//   agents    key slug                     { slug, order: [sessionId…], currentSessionId }
//   threads   key storageKey               { key, messages } — a palette / DM thread, named by
//                                            its localStorage key (which keeps only its contextId)
//
// The tab order and the focused chat live in `agents`: written only when they change, and never
// in localStorage, where a 50-id list would be another >1K value overwritten.
//
// One database for the origin. A backend (tenant) switch clears it, as it clears localStorage
// today (lib/tenant.ts).
//
// This is the ONLY module allowed to touch `indexedDB` (lib/storageGuard.test.ts).

import type { ChatMessage } from "../lib/types";
import { chunkHash, cutChunks } from "./transcriptChunks";

export const TRANSCRIPT_DB = "protoagent-transcripts";
// v2: + `agents` (tab order, focus) and `threads` (palette / DM threads). The upgrade only
// creates the stores a database lacks, so an older one gains them without losing anything.
const VERSION = 2;
/** Opening IndexedDB can stall (a blocked upgrade, a wedged WebKit storage process). Past this,
 *  the caller falls back to localStorage for the page's lifetime. */
export const OPEN_TIMEOUT_MS = 3_000;

type Message = ChatMessage;
export type SessionLike = { id: string; updatedAt: number; messages: Message[] } & Record<string, unknown>;

type SessionRecord = {
  slug: string;
  id: string;
  updatedAt: number;
  session: Record<string, unknown>;
  chunks: string[];
  tail: Message[];
};
type ChunkRecord = { slug: string; sessionId: string; hash: string; messages: Message[] };
export type AgentRecord = { slug: string; order: string[]; currentSessionId: string | null };
/** One saved session, as Settings → Storage counts it (`bytes`: JSON length of its records). */
export type SessionUsage = { slug: string; id: string; updatedAt: number; bytes: number };
export type ThreadUsage = { key: string; bytes: number };
/** What one eviction step removed: a session (`slug`/`id`) or a thread (`slug` "palette", `id`
 *  its key), with the JSON length of what went. */
export type Evicted = { slug: string; id: string; bytes: number };

function promised<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function done(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error ?? new DOMException("transaction aborted", "AbortError"));
  });
}

/** Rebuild a session from its record and chunks: chunks in order, then the tail. A missing
 *  chunk becomes one note at the top, never a silent gap (same rule as the localStorage form). */
function assemble(record: SessionRecord, chunks: Map<string, Message[]>): SessionLike {
  const earlier: Message[] = [];
  let missing = false;
  for (const hash of record.chunks) {
    const messages = chunks.get(hash);
    if (messages) earlier.push(...messages);
    else missing = true;
  }
  const note = missing
    ? [{
        id: `chunk-missing-${record.id}`,
        role: "system",
        content: "Earlier messages in this chat couldn't be loaded from this browser.",
        status: "done",
      } as Message]
    : [];
  return { ...record.session, id: record.id, updatedAt: record.updatedAt, messages: [...note, ...earlier, ...record.tail] } as SessionLike;
}

export class TranscriptStore {
  constructor(private readonly db: IDBDatabase) {}

  close(): void {
    this.db.close();
  }

  /** Every saved session of one agent, whole, in its saved tab order (sessions the order
   *  doesn't name follow, newest first), and its focused chat. */
  async loadAgent(slug: string): Promise<{ sessions: SessionLike[]; currentSessionId: string | null }> {
    const tx = this.db.transaction(["sessions", "chunks", "agents"], "readonly");
    const range = IDBKeyRange.bound([slug], [slug, []]);
    const [records, chunkRecords, agent] = await Promise.all([
      promised(tx.objectStore("sessions").getAll(range)) as Promise<SessionRecord[]>,
      promised(tx.objectStore("chunks").getAll(IDBKeyRange.bound([slug], [slug, []]))) as Promise<ChunkRecord[]>,
      promised(tx.objectStore("agents").get(slug)) as Promise<AgentRecord | undefined>,
    ]);
    const bySession = new Map<string, Map<string, Message[]>>();
    for (const chunk of chunkRecords) {
      const map = bySession.get(chunk.sessionId) ?? new Map<string, Message[]>();
      map.set(chunk.hash, chunk.messages);
      bySession.set(chunk.sessionId, map);
    }
    const position = new Map((agent?.order ?? []).map((id, i) => [id, i]));
    const sessions = records
      .map((record) => assemble(record, bySession.get(record.id) ?? new Map()))
      .sort((a, b) => {
        const pa = position.get(a.id);
        const pb = position.get(b.id);
        if (pa !== undefined && pb !== undefined) return pa - pb;
        if (pa !== undefined) return -1;
        if (pb !== undefined) return 1;
        return b.updatedAt - a.updatedAt;
      });
    return { sessions, currentSessionId: agent?.currentSessionId ?? null };
  }

  /** Save one agent's tab order and focused chat. */
  async putAgent(record: AgentRecord): Promise<void> {
    const tx = this.db.transaction("agents", "readwrite");
    tx.objectStore("agents").put(record);
    await done(tx);
  }

  /** Re-read some of one agent's sessions (another tab saved them). Missing ids are left out. */
  async loadSessions(slug: string, ids: string[]): Promise<SessionLike[]> {
    const tx = this.db.transaction(["sessions", "chunks"], "readonly");
    const out: SessionLike[] = [];
    for (const id of ids) {
      const record = (await promised(tx.objectStore("sessions").get([slug, id]))) as SessionRecord | undefined;
      if (!record) continue;
      const chunks = (await promised(
        tx.objectStore("chunks").getAll(IDBKeyRange.bound([slug, id], [slug, id, []])),
      )) as ChunkRecord[];
      out.push(assemble(record, new Map(chunks.map((c) => [c.hash, c.messages]))));
    }
    return out;
  }

  /** Save sessions of one agent: the chunks the store doesn't hold yet, the session records, and
   *  removal of chunks no record names any more. One transaction, so a reader never sees a record
   *  naming a missing chunk. Which chunks exist is read INSIDE that transaction, never from a
   *  cache: another tab may have deleted some (its eviction, or its own orphan collection), and a
   *  record naming a chunk this save didn't re-put would lose those messages. */
  async putSessions(slug: string, sessions: SessionLike[]): Promise<void> {
    if (!sessions.length) return;
    const tx = this.db.transaction(["sessions", "chunks"], "readwrite");
    const chunkStore = tx.objectStore("chunks");
    const sessionStore = tx.objectStore("sessions");
    const pending: Promise<unknown>[] = [];
    for (const session of sessions) {
      const { messages, ...rest } = session;
      const { chunks, tail } = cutChunks(messages);
      const named = chunks.map((chunk) => ({ chunk, hash: chunkHash(JSON.stringify(chunk)) }));
      const record: SessionRecord = { slug, id: session.id, updatedAt: session.updatedAt, session: rest, chunks: named.map((n) => n.hash), tail };
      const keep = new Set(record.chunks);
      const range = IDBKeyRange.bound([slug, session.id], [slug, session.id, []]);
      pending.push(
        promised(chunkStore.getAllKeys(range)).then((keys) => {
          const present = new Set((keys as [string, string, string][]).map((key) => key[2]));
          for (const { chunk, hash } of named) {
            if (!present.has(hash)) chunkStore.put({ slug, sessionId: session.id, hash, messages: chunk } satisfies ChunkRecord);
          }
          // This session's chunks no longer named (an edited earlier turn, a settled tail).
          for (const key of keys as [string, string, string][]) if (!keep.has(key[2])) chunkStore.delete(key);
          sessionStore.put(record);
        }),
      );
    }
    await Promise.all([...pending, done(tx)]);
  }

  /** Remove sessions of one agent and all their chunks. */
  async deleteSessions(slug: string, ids: string[]): Promise<void> {
    if (!ids.length) return;
    const tx = this.db.transaction(["sessions", "chunks"], "readwrite");
    for (const id of ids) {
      tx.objectStore("sessions").delete([slug, id]);
      tx.objectStore("chunks").delete(IDBKeyRange.bound([slug, id], [slug, id, []]));
    }
    await done(tx);
  }

  /** What the store holds, for Settings → Storage: every saved session (its agent, when it was
   *  last written, and its size) and every palette / DM thread. A size is the JSON length of
   *  the records (the session record plus its chunks), an estimate rather than disk use. */
  async usage(): Promise<{ sessions: SessionUsage[]; threads: ThreadUsage[] }> {
    const tx = this.db.transaction(["sessions", "chunks", "threads"], "readonly");
    const [records, chunks, threads] = await Promise.all([
      promised(tx.objectStore("sessions").getAll()) as Promise<SessionRecord[]>,
      promised(tx.objectStore("chunks").getAll()) as Promise<ChunkRecord[]>,
      promised(tx.objectStore("threads").getAll()) as Promise<{ key: string; messages: Message[] }[]>,
    ]);
    const chunkBytes = new Map<string, number>();
    for (const chunk of chunks) {
      const id = JSON.stringify([chunk.slug, chunk.sessionId]);
      chunkBytes.set(id, (chunkBytes.get(id) ?? 0) + JSON.stringify(chunk).length);
    }
    return {
      sessions: records.map((record) => ({
        slug: record.slug,
        id: record.id,
        updatedAt: record.updatedAt,
        bytes: JSON.stringify(record).length + (chunkBytes.get(JSON.stringify([record.slug, record.id])) ?? 0),
      })),
      threads: threads.map((thread) => ({ key: thread.key, bytes: JSON.stringify(thread).length })),
    };
  }

  /** Remove palette / DM threads by key. */
  async deleteThreads(keys: string[]): Promise<void> {
    if (!keys.length) return;
    const tx = this.db.transaction("threads", "readwrite");
    for (const key of keys) tx.objectStore("threads").delete(key);
    await done(tx);
  }

  /** Every agent with saved sessions, and how many. */
  async slugs(): Promise<Map<string, number>> {
    const tx = this.db.transaction("sessions", "readonly");
    const keys = (await promised(tx.objectStore("sessions").getAllKeys())) as [string, string][];
    const out = new Map<string, number>();
    for (const [slug] of keys) out.set(slug, (out.get(slug) ?? 0) + 1);
    return out;
  }

  /** Turns still streaming in OTHER agents' saved sessions (FleetTurnWatch): the last
   *  assistant bubble of each session's tail. A settled session has nothing in its tail. */
  async liveTurns(exceptSlug: string): Promise<{ slug: string; sessionId: string; taskId: string; title: string }[]> {
    const tx = this.db.transaction("sessions", "readonly");
    const records = (await promised(tx.objectStore("sessions").getAll())) as SessionRecord[];
    const out: { slug: string; sessionId: string; taskId: string; title: string }[] = [];
    for (const record of records) {
      if (record.slug === exceptSlug) continue;
      const last = [...record.tail].reverse().find((m) => m.role === "assistant");
      if (last?.status === "streaming" && last.taskId) {
        out.push({ slug: record.slug, sessionId: record.id, taskId: last.taskId, title: String(record.session.title || "a chat") });
      }
    }
    return out;
  }

  /** Every palette / DM thread whose key starts with `prefix`, by key. */
  async loadThreads(prefix: string): Promise<Map<string, Message[]>> {
    const tx = this.db.transaction("threads", "readonly");
    const records = (await promised(
      tx.objectStore("threads").getAll(IDBKeyRange.bound(prefix, `${prefix}\uffff`)),
    )) as { key: string; messages: Message[] }[];
    return new Map(records.map((r) => [r.key, r.messages]));
  }

  async putThread(key: string, messages: Message[]): Promise<void> {
    const tx = this.db.transaction("threads", "readwrite");
    tx.objectStore("threads").put({ key, messages });
    await done(tx);
  }

  /** Free space under IndexedDB quota pressure (ADR 0114 D5), one step per call, so the
   *  caller can retry its write after each. Order:
   *   1. every saved chat of agents no longer in the fleet;
   *   2. palette / DM threads of other windows;
   *   3. chats of other agents not written for `quietMs`, oldest first;
   *   4. this agent's oldest chats, one at a time, never one in `protect` (open tabs, live turns).
   *  Returns what it removed (empty when nothing is left to remove). */
  async evictStep(opts: {
    keepSlug: string;
    fleet: ReadonlySet<string> | null;
    protect: ReadonlySet<string>;
    threadPrefix: string;
    now?: number;
    quietMs?: number;
  }): Promise<Evicted[]> {
    const now = opts.now ?? Date.now();
    const quiet = opts.quietMs ?? 24 * 60 * 60 * 1000;
    const tx = this.db.transaction(["sessions", "threads"], "readonly");
    const records = (await promised(tx.objectStore("sessions").getAll())) as SessionRecord[];
    const threads = (await promised(tx.objectStore("threads").getAll())) as { key: string; messages: Message[] }[];
    const others = records.filter((r) => r.slug !== opts.keepSlug && r.slug !== "host");
    const gone = opts.fleet ? others.filter((r) => !opts.fleet!.has(r.slug)) : [];
    if (gone.length) return this.drop(gone);
    // This window's threads: its own key, or that key + ":dm:<member>". Everything else is foreign.
    const ownThread = (key: string) => key === opts.threadPrefix || key.startsWith(`${opts.threadPrefix}:dm:`);
    const foreignThreads = threads.filter((thread) => !ownThread(thread.key));
    if (foreignThreads.length) {
      await this.deleteThreads(foreignThreads.map((thread) => thread.key));
      return foreignThreads.map((thread) => ({ slug: "palette", id: thread.key, bytes: JSON.stringify(thread).length }));
    }
    const stale = others.filter((r) => now - r.updatedAt >= quiet).sort((a, b) => a.updatedAt - b.updatedAt);
    if (stale.length) return this.drop(stale);
    const mine = records
      .filter((r) => r.slug === opts.keepSlug && !opts.protect.has(r.id))
      .sort((a, b) => a.updatedAt - b.updatedAt);
    return mine.length ? this.drop(mine.slice(0, 1)) : [];
  }

  private async drop(records: SessionRecord[]): Promise<Evicted[]> {
    const sizes = await this.chunkBytes(records);
    const bySlug = new Map<string, string[]>();
    for (const r of records) bySlug.set(r.slug, [...(bySlug.get(r.slug) ?? []), r.id]);
    for (const [slug, ids] of bySlug) await this.deleteSessions(slug, ids);
    return records.map((r, i) => ({ slug: r.slug, id: r.id, bytes: JSON.stringify(r).length + sizes[i] }));
  }

  /** The JSON length of each record's chunks. */
  private async chunkBytes(records: SessionRecord[]): Promise<number[]> {
    const tx = this.db.transaction("chunks", "readonly");
    return Promise.all(
      records.map(async (r) => {
        const chunks = (await promised(
          tx.objectStore("chunks").getAll(IDBKeyRange.bound([r.slug, r.id], [r.slug, r.id, []])),
        )) as ChunkRecord[];
        return chunks.reduce((n, chunk) => n + JSON.stringify(chunk).length, 0);
      }),
    );
  }

  /** Remove everything (a backend switch, the crash reset). */
  async clear(): Promise<void> {
    const tx = this.db.transaction(["sessions", "chunks", "agents", "threads"], "readwrite");
    tx.objectStore("sessions").clear();
    tx.objectStore("chunks").clear();
    tx.objectStore("agents").clear();
    tx.objectStore("threads").clear();
    await done(tx);
  }
}

/** Open the transcript database, or `null` when IndexedDB is missing, refuses, or doesn't
 *  answer within `timeoutMs`. The caller then keeps localStorage for this page. */
export function openTranscriptStore(opts: { name?: string; timeoutMs?: number; factory?: IDBFactory } = {}): Promise<TranscriptStore | null> {
  let factory: IDBFactory | undefined;
  try {
    factory = opts.factory ?? globalThis.indexedDB;
  } catch {
    factory = undefined; // SecurityError: storage disabled for this origin
  }
  if (!factory) return Promise.resolve(null);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (store: TranscriptStore | null) => {
      if (settled) {
        store?.close();
        return;
      }
      settled = true;
      clearTimeout(timer);
      resolve(store);
    };
    const timer = setTimeout(() => finish(null), opts.timeoutMs ?? OPEN_TIMEOUT_MS);
    let request: IDBOpenDBRequest;
    try {
      request = factory!.open(opts.name ?? TRANSCRIPT_DB, VERSION);
    } catch {
      finish(null);
      return;
    }
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains("sessions")) db.createObjectStore("sessions", { keyPath: ["slug", "id"] });
      if (!db.objectStoreNames.contains("chunks")) db.createObjectStore("chunks", { keyPath: ["slug", "sessionId", "hash"] });
      if (!db.objectStoreNames.contains("agents")) db.createObjectStore("agents", { keyPath: "slug" });
      if (!db.objectStoreNames.contains("threads")) db.createObjectStore("threads", { keyPath: "key" });
    };
    request.onsuccess = () => {
      const db = request.result;
      // Another tab upgrading the schema: step aside rather than block it.
      db.onversionchange = () => db.close();
      finish(new TranscriptStore(db));
    };
    request.onerror = () => finish(null);
    request.onblocked = () => {
      /* wait: the timeout decides */
    };
  });
}

/** Clear every saved transcript, from a page that may not have the store open (AppCrash's
 *  reset, a backend switch before reload). Best-effort and bounded: resolves either way. */
/** Settle within `ms` whatever `work` does: a wedged IndexedDB must never keep a reset or a
 *  backend switch from reloading. */
export function withinMs<T>(work: Promise<T>, ms: number): Promise<T | undefined> {
  return Promise.race([work, new Promise<undefined>((resolve) => setTimeout(() => resolve(undefined), ms))]);
}

export async function clearTranscriptDatabase(opts: { factory?: IDBFactory; timeoutMs?: number } = {}): Promise<void> {
  await withinMs(clearTranscriptDatabaseNow(opts), opts.timeoutMs ?? OPEN_TIMEOUT_MS);
}

async function clearTranscriptDatabaseNow(opts: { factory?: IDBFactory; timeoutMs?: number }): Promise<void> {
  const store = await openTranscriptStore(opts);
  if (!store) return;
  try {
    await store.clear();
  } catch {
    /* best-effort */
  } finally {
    store.close();
  }
}
