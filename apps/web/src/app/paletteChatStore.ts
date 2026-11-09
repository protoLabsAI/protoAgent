// ADR 0057 — persistence for the command-palette chat. ONE preserved thread per
// agent: a stable A2A contextId (= the checkpointer thread_id `a2a:<id>` server-side,
// so server history survives too) + its transcript, in localStorage. Mirrors
// chat-store's slug-namespacing + try/catch + debounce. `/clear` mints a fresh thread
// and wipes the old one's checkpoints.
import type { ChatMessage } from "../lib/types";
import { listKeys, matchKey, readKey, writeKey } from "../lib/storage";
import { persistBlocked } from "../lib/storageReset";
import type { TranscriptStore } from "../chat/transcriptStore";
import { withQuotaEviction } from "../chat/chat-store";

// Per-agent key (ADR 0042 slug routing) — a window on /agent/<slug>/ keeps its own
// palette thread; host (no slug) uses the bare key. Fixed per page load.
const baseKey = (() => {
  try {
    const m = window.location.pathname.match(/\/agent\/([^/?#]+)/);
    return m ? `protoagent.palette.chat:${decodeURIComponent(m[1])}` : "protoagent.palette.chat";
  } catch {
    return "protoagent.palette.chat";
  }
})();

// A Fleet Room DM keeps its OWN thread per member (`scope = "dm:<slug>"`), so DMing
// different members doesn't cross-contaminate; the plain per-window chat passes no scope.
function keyFor(scope?: string): string {
  return scope ? `${baseKey}:${scope}` : baseKey;
}

export type PaletteThread = { contextId: string; messages: ChatMessage[] };

function newContextId(): string {
  return `palette-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// A corrupt persisted message must not white-screen the chat (cf. chat-store #872) —
// keep only well-formed ones. A message stuck "streaming" is settled to "done" UNLESS it
// carries a durable `taskId`: that one was interrupted mid-turn (palette closed), and
// PaletteChat's self-heal reconnects it to the server task on reopen — so keep it
// streaming for the reconnect to reconcile (mirrors the main chat, ChatSurface).
function sanitize(messages: unknown): ChatMessage[] {
  if (!Array.isArray(messages)) return [];
  return messages
    .filter((m): m is ChatMessage => {
      if (!m || typeof m !== "object") return false;
      const x = m as Record<string, unknown>;
      return (
        (x.role === "user" || x.role === "assistant" || x.role === "system") && typeof x.content === "string"
      );
    })
    .map((m) => (m.status === "streaming" && !m.taskId ? { ...m, status: "done" as const } : m));
}

// ── IndexedDB (epic #4981 S3) ─────────────────────────────────────────────────
// Once the transcript store is open, a thread's MESSAGES live in its `threads` store (keyed by
// the localStorage key) and the localStorage key keeps only `{contextId}`: short, written only
// when a new thread starts, so it never trips WebKit's >1K-overwrite WAL bug (#4974), and the
// server link survives a store that fails to open. This window's threads are preloaded before
// React mounts (main.tsx), so loadPaletteThread stays synchronous.
let threads: TranscriptStore | null = null;
const preloaded = new Map<string, ChatMessage[]>();
let threadWrites: Promise<void> = Promise.resolve();

/** Move every palette / DM thread's messages out of localStorage into the store (a copy the
 *  store already has wins), then preload this window's threads. Awaited at boot. */
export async function initPaletteThreads(store: TranscriptStore | null): Promise<void> {
  if (!store) return;
  const existing = await store.loadThreads("protoagent.palette.chat");
  for (const key of listKeys("local")) {
    if (matchKey("local", key)?.spec.id !== "palette.chat") continue;
    try {
      const p = JSON.parse(readKey("local", key) || "null") as Partial<PaletteThread> | null;
      if (!p || typeof p.contextId !== "string" || !Array.isArray(p.messages)) continue;
      if (!existing.has(key) && p.messages.length) await store.putThread(key, sanitize(p.messages));
      writeKey("local", key, JSON.stringify({ contextId: p.contextId }));
    } catch {
      /* corrupt: leave it for loadPaletteThread to discard */
    }
  }
  preloaded.clear();
  for (const [key, messages] of await store.loadThreads(baseKey)) preloaded.set(key, messages);
  threads = store;
}

export function loadPaletteThread(scope?: string): PaletteThread {
  try {
    const raw = readKey("local", keyFor(scope));
    if (raw) {
      const p = JSON.parse(raw) as Partial<PaletteThread>;
      if (p && typeof p.contextId === "string") {
        // The store's copy when it has one. Otherwise whatever is still in localStorage: a thread
        // whose migration didn't happen keeps its messages there, and showing nothing would lose
        // them at the next save.
        const stored = threads ? preloaded.get(keyFor(scope)) : undefined;
        const messages = stored ?? p.messages;
        return { contextId: p.contextId, messages: sanitize(messages) };
      }
    }
  } catch {
    // corrupt JSON / storage unavailable → fresh thread
  }
  return { contextId: newContextId(), messages: [] };
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
let pending: { thread: PaletteThread; scope?: string } | null = null;
function write(thread: PaletteThread, scope?: string) {
  // A storage reset (ADR 0114 D6) — never write a cleared thread back.
  if (persistBlocked()) return;
  try {
    const key = keyFor(scope);
    if (threads) {
      const store = threads;
      const link = JSON.stringify({ contextId: thread.contextId });
      preloaded.set(key, thread.messages);
      // The localStorage key shrinks to the link only once the store holds the messages. Until
      // then a key that still carries messages (a thread not yet moved) is kept whole and
      // current, so a failed store write never loses the only copy.
      if (hasMessages(readKey("local", key))) writeKey("local", key, JSON.stringify(thread));
      else if (readKey("local", key) !== link) writeKey("local", key, link);
      threadWrites = threadWrites
        .then(() => withQuotaEviction(() => store.putThread(key, thread.messages)))
        .then(() => {
          if (readKey("local", key) !== link) writeKey("local", key, link);
        })
        .catch((err) => console.error("[palette] saving the thread to IndexedDB failed", err));
      return;
    }
    writeKey("local", key, JSON.stringify(thread));
  } catch {
    // storage can be unavailable (hardened contexts)
  }
}

/** Persist the thread (optionally scoped to a DM target). Streaming frames coalesce on a
 *  trailing 300ms timer; pass `immediate` for structural changes (send start / clear).
 *  Only one palette chat is open at a time, so the single trailing timer flushes the
 *  latest thread+scope (`pending`). */
export function savePaletteThread(thread: PaletteThread, immediate = false, scope?: string): void {
  if (persistBlocked()) {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = null;
    pending = null;
    return;
  }
  pending = { thread, scope };
  if (immediate) {
    if (saveTimer) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
    write(thread, scope);
    pending = null;
    return;
  }
  if (saveTimer) return; // trailing write already scheduled
  saveTimer = setTimeout(() => {
    saveTimer = null;
    if (pending && !persistBlocked()) write(pending.thread, pending.scope);
    pending = null;
  }, 300);
}

/** A fresh, empty thread (new contextId) — persisted immediately. The caller wipes the
 *  OLD contextId's server checkpoints separately (api.deleteChatSession). */
export function freshPaletteThread(scope?: string): PaletteThread {
  const next: PaletteThread = { contextId: newContextId(), messages: [] };
  savePaletteThread(next, true, scope);
  return next;
}

/** Does this localStorage value still carry a thread's messages (not yet moved to the store)? */
function hasMessages(raw: string | null): boolean {
  try {
    return Array.isArray((JSON.parse(raw || "null") as Partial<PaletteThread> | null)?.messages);
  } catch {
    return false;
  }
}

/** Resolves when every queued thread save has committed (tests). */
export function paletteWritesSettled(): Promise<void> {
  return threadWrites;
}
