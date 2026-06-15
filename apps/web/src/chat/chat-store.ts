import { useSyncExternalStore } from "react";

import type { ChatMessage } from "../lib/types";

export const MAX_SESSIONS = 50;
export const MAX_ACTIVE_SESSIONS = 5;

export type ChatSession = {
  id: string;
  title: string;
  messages: ChatMessage[];
  createdAt: number;
  updatedAt: number;
  // Per-tab model override (gateway model id). Undefined → the configured
  // default. Sent with each turn so this tab talks to its own model.
  model?: string;
};

export type SessionStatus = "idle" | "streaming" | "error";

export type PersistedChatState = {
  version: number;
  sessions: ChatSession[];
  currentSessionId: string | null;
};

export type ChatState = PersistedChatState & {
  activeSessions: string[];
  sessionStatusMap: Record<string, SessionStatus>;
};

// Chat sessions are PER AGENT — namespace the persisted key by the URL slug (ADR 0042 slug
// routing), exactly like the per-agent layout. Without this every agent's window restores the
// same sessions from localStorage and you see one agent's chat under another. host (no /agent/
// slug) keeps the legacy un-suffixed key. The slug is fixed per page load (switching navigates).
const STORAGE_KEY = (() => {
  try {
    const m = window.location.pathname.match(/\/agent\/([^/?#]+)/);
    return m ? `protoagent.chat.sessions:${decodeURIComponent(m[1])}` : "protoagent.chat.sessions";
  } catch {
    return "protoagent.chat.sessions";
  }
})();

function id(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function titleFromMessages(messages: ChatMessage[]) {
  const text = messages.find((message) => message.role === "user")?.content.trim();
  if (!text) return "New chat";
  return text.length > 52 ? `${text.slice(0, 49)}...` : text;
}

function createSession(): ChatSession {
  const now = Date.now();
  return {
    id: id("chat"),
    title: "New chat",
    messages: [],
    createdAt: now,
    updatedAt: now,
  };
}

// A corrupt/hand-edited member must not reach render — it would throw past the
// panel boundaries and white-screen the app (#872). Only the fields render
// dereferences unconditionally are required; optional message fields can be
// anything (they're guarded at use).
function isValidSession(s: unknown): s is ChatSession {
  if (!s || typeof s !== "object") return false;
  const x = s as Record<string, unknown>;
  return (
    typeof x.id === "string" &&
    typeof x.title === "string" &&
    typeof x.createdAt === "number" &&
    typeof x.updatedAt === "number" &&
    Array.isArray(x.messages) &&
    x.messages.every((m) => {
      if (!m || typeof m !== "object") return false;
      const msg = m as Record<string, unknown>;
      return (
        (msg.role === "user" || msg.role === "assistant" || msg.role === "system") &&
        typeof msg.content === "string"
      );
    })
  );
}

/** Pure half of loadPersisted (unit-tested): drop invalid sessions, keep the rest,
 *  and re-point currentSessionId if it referenced a dropped one. Returns null when
 *  nothing usable survives (caller starts fresh). */
export function sanitizePersisted(parsed: unknown): PersistedChatState | null {
  if (!parsed || typeof parsed !== "object") return null;
  const p = parsed as Partial<PersistedChatState>;
  const sessions = (Array.isArray(p.sessions) ? p.sessions : [])
    .filter(isValidSession)
    .slice(0, MAX_SESSIONS);
  if (!sessions.length) return null;
  return {
    version: 1,
    sessions,
    currentSessionId: sessions.some((s) => s.id === p.currentSessionId)
      ? (p.currentSessionId as string)
      : sessions[0].id,
  };
}

function loadPersisted(): PersistedChatState {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const state = raw ? sanitizePersisted(JSON.parse(raw)) : null;
    if (state) return state;
  } catch {
    // Corrupt JSON or storage unavailable — fall through to a fresh session.
  }
  const session = createSession();
  return {
    version: 1,
    sessions: [session],
    currentSessionId: session.id,
  };
}

function persist(state: ChatState) {
  try {
    const payload: PersistedChatState = {
      version: state.version,
      sessions: state.sessions.slice(0, MAX_SESSIONS),
      currentSessionId: state.currentSessionId,
    };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // Storage can be unavailable in hardened browser contexts.
  }
}

// ── debounced persistence ─────────────────────────────────────────────────────
// The server flushes SSE every ~24 chars, and every streamed frame lands in
// updateMessages → setState. Serializing EVERY session to localStorage per frame
// is the dominant main-thread cost of a streaming turn (and each write fires a
// cross-window `storage` event that FleetTurnWatch re-parses). So streaming
// updates persist on a trailing ~300ms timer; structural changes (session
// add/remove/rename/switch, stream start/done) and page unload flush
// immediately. Only the localStorage WRITE is deferred — the in-memory state
// and listener notify stay synchronous, so the UI streams live.

export const PERSIST_DEBOUNCE_MS = 300;

let persistTimer: ReturnType<typeof setTimeout> | null = null;
let persistDirty = false;

function schedulePersist() {
  persistDirty = true;
  if (persistTimer !== null) return; // trailing write already scheduled
  persistTimer = setTimeout(() => {
    persistTimer = null;
    if (persistDirty) {
      persistDirty = false;
      persist(state);
    }
  }, PERSIST_DEBOUNCE_MS);
}

/** Write any pending (debounced) state to localStorage NOW. No-op when clean —
 * so a tenant-clear + reload (lib/tenant.ts) is never undone by an unload
 * flush that had nothing pending. Exported for tests + the unload hooks. */
export function flushChatPersist() {
  if (persistTimer !== null) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }
  if (persistDirty) {
    persistDirty = false;
    persist(state);
  }
}

// pagehide covers bfcache navigations Safari/iOS never fire beforeunload for;
// beforeunload covers older flows. flushChatPersist is idempotent.
try {
  window.addEventListener("pagehide", flushChatPersist);
  window.addEventListener("beforeunload", flushChatPersist);
} catch {
  // non-browser context (tests without a full window)
}

export function ensureActiveSessions(state: ChatState, sessionId: string | null): string[] {
  if (!sessionId) return state.activeSessions;
  if (state.activeSessions.includes(sessionId)) return state.activeSessions;

  const next = [...state.activeSessions, sessionId];
  if (next.length <= MAX_ACTIVE_SESSIONS) return next;

  const removable = next.findIndex(
    (id) => id !== sessionId && state.sessionStatusMap[id] !== "streaming",
  );
  if (removable >= 0) next.splice(removable, 1);
  else next.shift();
  return next;
}

let initial = loadPersisted();
let state: ChatState = {
  ...initial,
  activeSessions: initial.currentSessionId ? [initial.currentSessionId] : [],
  sessionStatusMap: {},
};

const listeners = new Set<() => void>();

function setState(
  updater: (current: ChatState) => ChatState,
  persistMode: "immediate" | "debounced" = "immediate",
) {
  state = updater(state);
  if (persistMode === "immediate") {
    persistDirty = true;
    flushChatPersist(); // cancels any pending timer and writes the full state
  } else {
    schedulePersist();
  }
  listeners.forEach((listener) => listener());
}

export const chatStore = {
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },

  getSnapshot() {
    return state;
  },

  createSession() {
    const session = createSession();
    setState((current) => {
      // New tabs append to the RIGHT; cap at MAX_SESSIONS by dropping the oldest (left).
      const sessions = [...current.sessions, session].slice(-MAX_SESSIONS);
      return {
        ...current,
        sessions,
        currentSessionId: session.id,
        activeSessions: ensureActiveSessions(
          { ...current, sessions, currentSessionId: session.id },
          session.id,
        ),
      };
    });
    return session;
  },

  deleteSession(sessionId: string) {
    setState((current) => {
      const sessions = current.sessions.filter((session) => session.id !== sessionId);
      const currentSessionId =
        current.currentSessionId === sessionId ? sessions[0]?.id || null : current.currentSessionId;
      const sessionStatusMap = { ...current.sessionStatusMap };
      delete sessionStatusMap[sessionId];
      return {
        ...current,
        sessions,
        currentSessionId,
        activeSessions: ensureActiveSessions(
          {
            ...current,
            sessions,
            currentSessionId,
            activeSessions: current.activeSessions.filter((id) => id !== sessionId),
            sessionStatusMap,
          },
          currentSessionId,
        ),
        sessionStatusMap,
      };
    });
  },

  switchSession(sessionId: string) {
    setState((current) => ({
      ...current,
      currentSessionId: sessionId,
      activeSessions: ensureActiveSessions(current, sessionId),
    }));
  },

  updateMessages(sessionId: string, messages: ChatMessage[]) {
    // Fires per streamed SSE frame (~24 chars) — debounce the localStorage
    // write. The stream-done path flushes via setSessionStatus right after the
    // final updateMessages, so the terminal state always lands immediately.
    setState(
      (current) => ({
        ...current,
        sessions: current.sessions.map((session) =>
          session.id === sessionId
            ? {
                ...session,
                title: session.title === "New chat" ? titleFromMessages(messages) : session.title,
                messages,
                updatedAt: Date.now(),
              }
            : session,
        ),
      }),
      "debounced",
    );
  },

  renameSession(sessionId: string, title: string) {
    setState((current) => ({
      ...current,
      sessions: current.sessions.map((session) =>
        session.id === sessionId ? { ...session, title: title.trim() || "New chat" } : session,
      ),
    }));
  },

  setSessionStatus(sessionId: string, status: SessionStatus) {
    setState((current) => ({
      ...current,
      sessionStatusMap: { ...current.sessionStatusMap, [sessionId]: status },
    }));
  },

  // Per-tab model override. Empty string clears it (→ configured default).
  setSessionModel(sessionId: string, model: string) {
    setState((current) => ({
      ...current,
      sessions: current.sessions.map((session) =>
        session.id === sessionId ? { ...session, model: model || undefined } : session,
      ),
    }));
  },
};

export function useChatState() {
  return useSyncExternalStore(chatStore.subscribe, chatStore.getSnapshot, chatStore.getSnapshot);
}

// Narrow selector: is ANY session mid-stream? Returns a primitive so subscribers
// (e.g. the nav rail's background-streaming dot) re-render only when the boolean
// flips — not on every streamed token. Drives the "chat is progressing while
// you're on another tab" indicator.
const _anyStreaming = () =>
  Object.values(chatStore.getSnapshot().sessionStatusMap).some((s) => s === "streaming");
export function useAnyChatStreaming(): boolean {
  return useSyncExternalStore(chatStore.subscribe, _anyStreaming, () => false);
}
