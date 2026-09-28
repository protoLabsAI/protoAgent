// ADR 0057 — persistence for the command-palette chat. ONE preserved thread per
// agent: a stable A2A contextId (= the checkpointer thread_id `a2a:<id>` server-side,
// so server history survives too) + its transcript, in localStorage. Mirrors
// chat-store's slug-namespacing + try/catch + debounce. `/clear` mints a fresh thread
// and wipes the old one's checkpoints.
import type { ChatMessage } from "../lib/types";
import { readKeyOrThrow, writeKey } from "../lib/storage";
import { persistBlocked } from "../lib/storageReset";

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

export function loadPaletteThread(scope?: string): PaletteThread {
  try {
    const stored = readStoredPaletteThread(scope);
    if (stored) return stored;
  } catch {
    // storage unavailable → fresh thread
  }
  return { contextId: newContextId(), messages: [] };
}

/** The stored thread, or `null` when none is stored (or the blob is corrupt — nothing
 *  in it is recoverable). THROWS when storage itself can't be read: that is a failed
 *  read, not an empty one, and the caller must not treat it as "no thread". */
export function readStoredPaletteThread(scope?: string): PaletteThread | null {
  const raw = readKeyOrThrow("local", keyFor(scope));
  if (!raw) return null;
  try {
    const p = JSON.parse(raw) as Partial<PaletteThread>;
    if (p && typeof p.contextId === "string") return { contextId: p.contextId, messages: sanitize(p.messages) };
  } catch {
    // corrupt JSON → nothing recoverable
  }
  return null;
}

/** The thread's stored contextId alone — the server link (ADR 0114 D2 keeps it in
 *  localStorage even once transcripts move to IndexedDB, so it survives a slow or failed
 *  transcript read). `null` when none is stored or storage can't be read. */
export function readPaletteContextId(scope?: string): string | null {
  try {
    return readStoredPaletteThread(scope)?.contextId ?? null;
  } catch {
    return null;
  }
}

export { newContextId as newPaletteContextId };

// ── load barrier (ADR 0114 D2) ──────────────────────────────────────────────────
// PaletteChat must not save, self-heal or auto-send before its thread has loaded. Today
// the read is synchronous (the default loader); S5 swaps in an IndexedDB read, and tests
// install a deferred one. A pending read times out to `failed` after
// PALETTE_LOAD_TIMEOUT_MS; a late success still loads it.

export const PALETTE_LOAD_TIMEOUT_MS = 10_000;

/** Reads a thread: the thread, `null` for none stored, or a rejection/throw for a failed
 *  read. May answer synchronously or with a promise. */
export type PaletteThreadLoader = (scope?: string) => PaletteThread | null | Promise<PaletteThread | null>;

// Blocked storage (a hardened context: getItem throws SecurityError) is NOT a failed read
// here — there is nothing to protect and nothing that ever will load, so it starts a fresh
// thread, as the palette always has. `failed` is reserved for an asynchronous loader,
// whose record exists and may still load.
const defaultPaletteThreadLoader: PaletteThreadLoader = (scope) => {
  try {
    return readStoredPaletteThread(scope);
  } catch {
    return null;
  }
};
let paletteThreadLoader: PaletteThreadLoader = defaultPaletteThreadLoader;

/** Install the thread loader (the S5 seam; tests pass a deferred one). `null` restores
 *  the synchronous localStorage read. */
export function setPaletteThreadLoader(loader: PaletteThreadLoader | null): void {
  paletteThreadLoader = loader ?? defaultPaletteThreadLoader;
}

export type PaletteThreadLoad =
  | { state: "loaded"; thread: PaletteThread | null }
  | { state: "pending"; promise: Promise<PaletteThread | null> }
  | { state: "failed" };

/** Start reading a thread. A synchronous loader answers `loaded` (or `failed`) at once,
 *  so the common path renders its transcript on the first paint. */
export function beginPaletteThreadLoad(scope?: string): PaletteThreadLoad {
  try {
    const result = paletteThreadLoader(scope);
    if (result && typeof (result as Promise<unknown>).then === "function") {
      return { state: "pending", promise: result as Promise<PaletteThread | null> };
    }
    return { state: "loaded", thread: result as PaletteThread | null };
  } catch {
    return { state: "failed" };
  }
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
let pending: { thread: PaletteThread; scope?: string } | null = null;
function write(thread: PaletteThread, scope?: string) {
  // A storage reset (ADR 0114 D6) — never write a cleared thread back.
  if (persistBlocked()) return;
  try {
    writeKey("local", keyFor(scope), JSON.stringify(thread));
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
