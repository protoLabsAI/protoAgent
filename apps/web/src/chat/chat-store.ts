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
};

export type SessionStatus = "idle" | "streaming" | "error";

type PersistedChatState = {
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

function loadPersisted(): PersistedChatState {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) throw new Error("empty");
    const parsed = JSON.parse(raw) as Partial<PersistedChatState>;
    const sessions = Array.isArray(parsed.sessions) ? parsed.sessions.slice(0, MAX_SESSIONS) : [];
    return {
      version: 1,
      sessions,
      currentSessionId: parsed.currentSessionId || sessions[0]?.id || null,
    };
  } catch {
    const session = createSession();
    return {
      version: 1,
      sessions: [session],
      currentSessionId: session.id,
    };
  }
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

function setState(updater: (current: ChatState) => ChatState) {
  state = updater(state);
  persist(state);
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
    setState((current) => ({
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
    }));
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
