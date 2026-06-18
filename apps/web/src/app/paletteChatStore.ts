// ADR 0057 — persistence for the command-palette chat. ONE preserved thread per
// agent: a stable A2A contextId (= the checkpointer thread_id `a2a:<id>` server-side,
// so server history survives too) + its transcript, in localStorage. Mirrors
// chat-store's slug-namespacing + try/catch + debounce. `/clear` mints a fresh thread
// and wipes the old one's checkpoints.
import type { ChatMessage } from "../lib/types";

// Per-agent key (ADR 0042 slug routing) — a window on /agent/<slug>/ keeps its own
// palette thread; host (no slug) uses the bare key. Fixed per page load.
const KEY = (() => {
  try {
    const m = window.location.pathname.match(/\/agent\/([^/?#]+)/);
    return m ? `protoagent.palette.chat:${decodeURIComponent(m[1])}` : "protoagent.palette.chat";
  } catch {
    return "protoagent.palette.chat";
  }
})();

export type PaletteThread = { contextId: string; messages: ChatMessage[] };

function newContextId(): string {
  return `palette-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

// A corrupt persisted message must not white-screen the chat (cf. chat-store #872) —
// keep only well-formed ones, and never leave a message stuck "streaming" on reload.
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
    .map((m) => (m.status === "streaming" ? { ...m, status: "done" as const } : m));
}

export function loadPaletteThread(): PaletteThread {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw) {
      const p = JSON.parse(raw) as Partial<PaletteThread>;
      if (p && typeof p.contextId === "string") {
        return { contextId: p.contextId, messages: sanitize(p.messages) };
      }
    }
  } catch {
    // corrupt JSON / storage unavailable → fresh thread
  }
  return { contextId: newContextId(), messages: [] };
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
function write(thread: PaletteThread) {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(thread));
  } catch {
    // storage can be unavailable (hardened contexts)
  }
}

/** Persist the thread. Streaming frames coalesce on a trailing 300ms timer; pass
 *  `immediate` for structural changes (send start / clear). */
export function savePaletteThread(thread: PaletteThread, immediate = false): void {
  if (immediate) {
    if (saveTimer) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
    write(thread);
    return;
  }
  if (saveTimer) return; // trailing write already scheduled
  saveTimer = setTimeout(() => {
    saveTimer = null;
    write(thread);
  }, 300);
}

/** A fresh, empty thread (new contextId) — persisted immediately. The caller wipes the
 *  OLD contextId's server checkpoints separately (api.deleteChatSession). */
export function freshPaletteThread(): PaletteThread {
  const next: PaletteThread = { contextId: newContextId(), messages: [] };
  savePaletteThread(next, true);
  return next;
}
