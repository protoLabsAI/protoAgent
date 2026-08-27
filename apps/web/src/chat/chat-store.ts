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
  // Per-tab reasoning effort (the /effort command). Undefined → the default
  // (DEFAULT_REASONING_EFFORT, auto-enabled); "off" → no reasoning this tab;
  // else low|medium|high|max. Sent each turn so the tab reasons at its own level.
  reasoningEffort?: string;
  // Per-tab "bypass permissions" mode (the /bypass command): when true, each turn carries
  // metadata.bypass_permissions so the server auto-approves run_command (no HITL gate). A
  // deliberately dangerous escape hatch — default OFF; the composer shows a loud chip while on.
  bypassPermissions?: boolean;
  // Per-tab incognito mode (ADR 0069 D3b): while ON, EVERY message sent from this tab
  // carries metadata.incognito — the server skips memory persistence AND injection for
  // that turn. The flag is per-MESSAGE server-side, so it must ride every send: a mixed
  // thread would leak earlier incognito content into a later non-incognito turn's summary.
  incognito?: boolean;
  // Group chat (#3049): the delegates this room is with, by their `@`-addressable
  // names. Undefined/empty = an ordinary chat with the lead agent alone. The list
  // ORDERS the `@` popover rather than gating it — a room's membership is a
  // convenience, and refusing to route `@somebody-else` would be a surprise, not a
  // safety property.
  participants?: string[];
};

// Reasoning is ON by default in the console (auto-enable) — a fresh tab thinks at
// this level until the operator changes it with /effort. "off" disables it per-tab.
export const DEFAULT_REASONING_EFFORT = "medium";
export const REASONING_EFFORTS = ["off", "low", "medium", "high", "max"] as const;

/** The effort to actually send for a tab: the tab's explicit pick, or the default.
 *  "off" → undefined so the turn carries no effort (the model's own default). */
export function effectiveReasoningEffort(session?: { reasoningEffort?: string } | null): string | undefined {
  const e = session?.reasoningEffort ?? DEFAULT_REASONING_EFFORT;
  return e === "off" ? undefined : e;
}

export type SessionStatus = "idle" | "streaming" | "error";

export type PersistedChatState = {
  version: number;
  sessions: ChatSession[];
  currentSessionId: string | null;
};

export type ChatState = PersistedChatState & {
  activeSessions: string[];
  sessionStatusMap: Record<string, SessionStatus>;
  // A delete asked for from OUTSIDE the tab strip (the mobile SessionSheet). Ephemeral,
  // never persisted. ChatSurface owns the one "Delete this chat?" ConfirmDialog — with
  // its Harvest opt-in, server-side purge, and goal Stop-vs-Detach fork — so surfaces
  // that can't render that dialog park the id here instead of deleting directly (#2512).
  pendingDeleteRequest: string | null;
  // A "clear this conversation" asked for from a surface that can't render a dialog —
  // the ⌘K chat.clear keybinding and the /clear slash command, both of which run OUTSIDE
  // React (#2996). ChatSurface owns the "Clear this conversation?" ConfirmDialog (Harvest
  // opt-in, same as delete). Distinct from the delete request: clear wipes the history but
  // KEEPS the tab open, so it can't ride the delete-request path. Ephemeral, never persisted.
  pendingClearRequest: string | null;
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

// Sessions this tab deleted — a lightweight per-tab tombstone so the cross-tab merge
// never resurrects a chat we just removed from another tab's stale on-disk copy.
const locallyDeletedIds = new Set<string>();

/** Identity token captured immediately before a durable turn read. Any local
 * edit replaces the session object, so the exact reference is a cheap per-tab
 * generation: delete, clear, send, rename, or settings changes while the read
 * is in flight veto the stale recovery result. */
export type HydrationEligibility = {
  sessionId: string;
  localSession: ChatSession | null;
};

// ── goal kickoff seam ──────────────────────────────────────────────────────────
// When a goal is created from the Work panel we open a dedicated tab and drive the goal
// FROM it (so the loop streams live) rather than as a headless background turn. The
// ChatSessionSlot for that session fires the first (hidden) turn — but it mounts BEFORE the
// goal is set on the server (the POST is in flight), so a plain mount-effect would race
// ahead. This tiny pub/sub lets the caller register the kickoff AFTER the goal POST resolves;
// the slot (subscribed) then fires it exactly once (takeGoalKickoff is idempotent).
const pendingGoalKickoffs = new Map<string, string>();
const kickoffListeners = new Set<() => void>();

/** Queue a one-shot kickoff turn for `sessionId` (call after the goal is set) and notify the
 *  slot to consume it. `prompt` seeds the turn; the server's iteration-0 kickoff injection
 *  re-states the goal, so it drives from there. */
export function registerGoalKickoff(sessionId: string, prompt: string) {
  pendingGoalKickoffs.set(sessionId, prompt);
  kickoffListeners.forEach((l) => l());
}

/** Subscribe to kickoff registrations (the ChatSessionSlot). Returns an unsubscribe fn. */
export function subscribeGoalKickoff(listener: () => void): () => void {
  kickoffListeners.add(listener);
  return () => kickoffListeners.delete(listener);
}

/** Consume `sessionId`'s pending kickoff prompt (removing it), or null if none. */
export function takeGoalKickoff(sessionId: string): string | null {
  const prompt = pendingGoalKickoffs.get(sessionId);
  if (prompt != null) pendingGoalKickoffs.delete(sessionId);
  return prompt ?? null;
}

function id(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** The title a session carries until its first user message renames it (or the operator
 *  does). Load-bearing beyond display: `unusedSession` treats it as "never used", and the
 *  auto-title on first reply only fires while the title is still this. */
export const DEFAULT_SESSION_TITLE = "New chat";

function titleFromMessages(messages: ChatMessage[]) {
  const text = messages.find((message) => message.role === "user")?.content.trim();
  if (!text) return DEFAULT_SESSION_TITLE;
  return text.length > 52 ? `${text.slice(0, 49)}...` : text;
}

function createSession(opts: { incognito?: boolean } = {}): ChatSession {
  const now = Date.now();
  return {
    id: id("chat"),
    title: DEFAULT_SESSION_TITLE,
    messages: [],
    createdAt: now,
    updatedAt: now,
    ...(opts.incognito ? { incognito: true } : {}),
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
    .slice(0, MAX_SESSIONS)
    // A duplicate entry that made it into a persisted blob (#1938) collapses on load.
    .map((s) => {
      const messages = dedupeMessages(s.messages);
      return messages === s.messages ? s : { ...s, messages };
    });
  if (!sessions.length) return null;
  return {
    version: 1,
    sessions,
    currentSessionId: sessions.some((s) => s.id === p.currentSessionId)
      ? (p.currentSessionId as string)
      : sessions[0].id,
  };
}

/** Merge two session lists from different tabs that share one localStorage key (same
 *  agent slug). Union by id; for an id in BOTH, the newer `updatedAt` wins — EXCEPT a
 *  session this tab is actively streaming (its in-memory copy is the freshest; never
 *  overwrite a live stream with a cross-tab snapshot) and a session this tab deleted
 *  (`deletedIds` — never resurrect it from another tab's copy). Order: this tab's order
 *  first, then sessions only the other tab has (oldest-created first).
 *
 *  This is the fix for the last-writer-wins clobber, where one tab's full-store write
 *  dropped another tab's chats. Cross-tab DELETE is best-effort: a chat deleted in one
 *  tab can linger in another until that tab removes it too (no shared tombstones). */
export function mergeSessions(
  local: ChatSession[],
  incoming: ChatSession[],
  opts: { streamingIds?: Set<string>; deletedIds?: Set<string> } = {},
): ChatSession[] {
  const streaming = opts.streamingIds ?? new Set<string>();
  const deleted = opts.deletedIds ?? new Set<string>();
  const byId = new Map<string, ChatSession>();
  for (const s of local) byId.set(s.id, s);
  for (const s of incoming) {
    if (deleted.has(s.id)) continue;
    const mine = byId.get(s.id);
    if (!mine) byId.set(s.id, s);
    else if (!streaming.has(s.id) && s.updatedAt > mine.updatedAt) byId.set(s.id, s);
  }
  const out: ChatSession[] = [];
  const seen = new Set<string>();
  for (const s of local) {
    if (deleted.has(s.id) || seen.has(s.id)) continue;
    out.push(byId.get(s.id)!);
    seen.add(s.id);
  }
  for (const s of [...incoming].sort((a, b) => a.createdAt - b.createdAt)) {
    if (deleted.has(s.id) || seen.has(s.id)) continue;
    out.push(byId.get(s.id)!);
    seen.add(s.id);
  }
  return out.slice(0, MAX_SESSIONS);
}

/** Fold server-recovered sessions into the local-first store (#2888).
 * Non-empty local transcripts always win because they carry richer ordered
 * parts and client-only annotations. A locally empty copy may be recovered,
 * and server-only sessions fill only the remaining cap. The sole auto-created
 * blank tab is a boot placeholder, so a successful recovery replaces it. */
export function mergeHydratedSessions(current: ChatState, incoming: ChatSession[]): ChatState {
  if (!incoming.length) return current;
  const solePlaceholder = current.sessions.length === 1 ? unusedSession(current) : undefined;
  const removePlaceholder = Boolean(solePlaceholder && incoming.some((session) => session.messages.length > 0));
  const local = removePlaceholder
    ? current.sessions.filter((session) => session.id !== solePlaceholder?.id)
    : [...current.sessions];
  const byId = new Map(local.map((session) => [session.id, session]));
  const hydratedIds = new Set<string>();

  for (const recovered of incoming) {
    if (!recovered.messages.length) continue;
    const existing = byId.get(recovered.id);
    if (existing?.messages.length) continue;
    if (existing) {
      const next = {
        ...recovered,
        // A manually named empty tab expresses local intent; retain its title
        // while filling only the missing transcript.
        title: existing.title === DEFAULT_SESSION_TITLE ? recovered.title : existing.title,
        model: existing.model,
        reasoningEffort: existing.reasoningEffort,
        bypassPermissions: existing.bypassPermissions,
        // Local intent wins when explicitly present; an empty placeholder with
        // no local choice inherits the newest durable operator metadata.
        incognito: existing.incognito ?? recovered.incognito,
        participants: existing.participants,
        createdAt: Math.min(existing.createdAt, recovered.createdAt),
      };
      byId.set(recovered.id, next);
      hydratedIds.add(recovered.id);
    }
  }

  const existingIds = new Set(local.map((session) => session.id));
  const slots = Math.max(0, MAX_SESSIONS - local.length);
  const missing = incoming
    .filter((session) => session.messages.length > 0 && !existingIds.has(session.id))
    .sort((a, b) => a.updatedAt - b.updatedAt);
  const additions = slots ? missing.slice(-slots) : [];
  for (const session of additions) {
    byId.set(session.id, session);
    hydratedIds.add(session.id);
  }

  const sessions = [
    ...local.map((session) => byId.get(session.id) ?? session),
    ...additions,
  ];
  if (!hydratedIds.size) return current;
  const currentSessionId = removePlaceholder
    ? (additions[additions.length - 1]?.id ?? sessions[sessions.length - 1]?.id ?? null)
    : current.currentSessionId;
  const activeSessions = ensureActiveSessions(
    {
      ...current,
      sessions,
      activeSessions: current.activeSessions.filter((id) => sessions.some((session) => session.id === id)),
    },
    currentSessionId,
  );
  const sessionStatusMap = { ...current.sessionStatusMap };
  if (solePlaceholder) delete sessionStatusMap[solePlaceholder.id];
  for (const session of sessions) {
    if (!hydratedIds.has(session.id)) continue;
    const last = [...session.messages].reverse().find((message) => message.role === "assistant");
    if (last?.status === "streaming" && last.taskId) sessionStatusMap[session.id] = "streaming";
    else if (last?.status === "error") sessionStatusMap[session.id] = "error";
  }
  return { ...current, sessions, currentSessionId, activeSessions, sessionStatusMap };
}

/** Collapse duplicate message entries by id, keeping the LAST occurrence (the
 *  freshest write wins — a finalize/reconcile rewrite supersedes the copy it was
 *  derived from). Id-less legacy entries pass through untouched. This is the
 *  store-boundary guarantee behind #1938: whatever interleaving produced a
 *  duplicate entry upstream, it can never persist or render. */
export function dedupeMessages(messages: ChatMessage[]): ChatMessage[] {
  if (!messages.some((m, i) => m.id && messages.findIndex((o) => o.id === m.id) !== i)) return messages;
  const lastIndexById = new Map<string, number>();
  messages.forEach((m, i) => {
    if (m.id) lastIndexById.set(m.id, i);
  });
  return messages.filter((m, i) => !m.id || lastIndexById.get(m.id) === i);
}

function streamingIds(state: ChatState): Set<string> {
  return new Set(Object.keys(state.sessionStatusMap).filter((id) => state.sessionStatusMap[id] === "streaming"));
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
    // Read-merge-write: a concurrent tab sharing this key may have written sessions we
    // don't have (or newer copies) since our last read. Fold them in so our write never
    // clobbers another tab's chats (the last-writer-wins data-loss bug). Our own
    // streaming sessions stay authoritative; locally-deleted ones are not resurrected.
    let sessions = state.sessions;
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      const onDisk = raw ? sanitizePersisted(JSON.parse(raw)) : null;
      if (onDisk) {
        sessions = mergeSessions(state.sessions, onDisk.sessions, {
          streamingIds: streamingIds(state),
          deletedIds: locallyDeletedIds,
        });
      }
    } catch {
      // Corrupt on-disk blob — write our own state rather than lose this turn.
    }
    const payload: PersistedChatState = {
      version: state.version,
      // Ephemeral overlays (/btw asides, #2483) are display-only: stripping them
      // HERE — not from live state — keeps them on screen for the session while
      // guaranteeing a reload forgets them, which is what "saved nowhere" means.
      sessions: sessions.slice(0, MAX_SESSIONS).map((s) =>
        s.messages.some((m) => m.ephemeral)
          ? { ...s, messages: s.messages.filter((m) => !m.ephemeral) }
          : s,
      ),
      currentSessionId: state.currentSessionId,
    };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // Storage can be unavailable in hardened browser contexts.
  }
}

// ── debounced persistence ─────────────────────────────────────────────────────
// The server flushes SSE every ~24 chars, and every streamed frame lands in
// updateMessages → setState — as does every tick of the client reveal queue
// (#2993), which re-paces answer text into ~word-sized updates up to ~30/s.
// Serializing EVERY session to localStorage per update
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

/**
 * A session that has never been used, and so is interchangeable with a freshly created
 * one: no messages and still carrying the default title. Drives both the reuse guard in
 * `chatStore.createSession` and the disabled state of every "new chat" affordance.
 *
 * A RENAMED empty session is deliberately excluded — naming it is intent ("Ideas"), and
 * silently landing the operator there when they asked for a new chat would be a surprise.
 * Incognito must match too: an incognito request is a different kind of session, so a
 * plain blank can't satisfy it (ADR 0069 D3b — incognito threads leave no memory).
 */
export function unusedSession(
  state: ChatState,
  opts: { incognito?: boolean } = {},
): ChatSession | undefined {
  const wantIncognito = Boolean(opts.incognito);
  return state.sessions.find(
    (s) =>
      s.messages.length === 0 &&
      s.title === DEFAULT_SESSION_TITLE &&
      Boolean(s.incognito) === wantIncognito,
  );
}

/** Who is in this chat (#3049) — DERIVED from the transcript, never tracked from
 *  keystrokes. A participant is a delegate that has spoken here (an authored assistant
 *  message), in first-spoken order. Deriving is the whole design: a tracked list can
 *  only drift from the transcript (chips that outlive a deleted draft were the bug that
 *  killed the first cut), and nothing in this system "listens" — an agent acts only
 *  when addressed — so the strip is a record of the conversation's cast, not presence. */
export function sessionCast(session: Pick<ChatSession, "messages"> | null | undefined): string[] {
  const seen: string[] = [];
  for (const m of session?.messages ?? []) {
    const name = m.role === "assistant" ? m.author?.name : undefined;
    if (name && !seen.includes(name)) seen.push(name);
  }
  return seen;
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

// Swap & Resume S2 — derive live status from the persisted transcripts instead
// of booting blind. A session whose last assistant message is still `streaming`
// with a durable taskId has a server-owned turn in flight: it must come back
// ACTIVE (so its slot mounts and the reattach fires — previously only the
// focused tab reconciled) and marked `streaming` (so the composer stays locked
// and Stop stays visible — previously the map reset to {} and the operator
// could fire a second concurrent turn into the same session). Derivation, not
// persistence: the transcript is already the durable truth, and a persisted
// status map could lie about a turn that ended while the tab was closed —
// the reattach settles each derived `streaming` to idle/error from the task.
function sessionsWithLiveTurns(persisted: PersistedChatState): string[] {
  return persisted.sessions
    .filter((session) => {
      const last = [...session.messages].reverse().find((m) => m.role === "assistant");
      return last?.status === "streaming" && !!last.taskId;
    })
    .map((session) => session.id);
}

const resumeIds = sessionsWithLiveTurns(initial);
let state: ChatState = {
  ...initial,
  activeSessions: [
    ...(initial.currentSessionId ? [initial.currentSessionId] : []),
    ...resumeIds.filter((id) => id !== initial.currentSessionId),
  ].slice(0, MAX_ACTIVE_SESSIONS),
  sessionStatusMap: Object.fromEntries(resumeIds.map((id) => [id, "streaming" as SessionStatus])),
  pendingDeleteRequest: null,
  pendingClearRequest: null,
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

/** A sibling tab sharing our localStorage key wrote new chat state. Merge its sessions
 *  into ours (so its new/updated chats show up here live) WITHOUT disturbing this tab's
 *  own view — our currentSessionId, active tabs, and live stream statuses stay put. We
 *  don't persist here: the next real write carries the union via persist()'s
 *  read-merge-write, so two tabs can't ping-pong writes. */
function mergeFromStorage(incoming: PersistedChatState) {
  const sessions = mergeSessions(state.sessions, incoming.sessions, {
    streamingIds: streamingIds(state),
    deletedIds: locallyDeletedIds,
  });
  const currentSessionId =
    state.currentSessionId && sessions.some((s) => s.id === state.currentSessionId)
      ? state.currentSessionId
      : (sessions[0]?.id ?? null);
  const activeSessions = state.activeSessions.filter((id) => sessions.some((s) => s.id === id));
  state = { ...state, sessions, currentSessionId, activeSessions };
  listeners.forEach((listener) => listener());
}

try {
  window.addEventListener("storage", (e: StorageEvent) => {
    if (e.key !== STORAGE_KEY || e.newValue == null) return; // other key, or a tenant-clear
    try {
      const incoming = sanitizePersisted(JSON.parse(e.newValue));
      if (incoming) mergeFromStorage(incoming);
    } catch {
      // Ignore a malformed cross-tab write — our own state is unaffected.
    }
  });
} catch {
  // non-browser context (tests without a full window)
}

export const chatStore = {
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },

  getSnapshot() {
    return state;
  },

  createSession(opts: { incognito?: boolean } = {}) {
    // Don't pile up blanks. A pristine session is byte-for-byte what this call would
    // produce, so hand it back instead — otherwise tapping "+" repeatedly (easy to do
    // now that it's a primary action in the mobile header) spawns identical empty tabs
    // that the operator then has to close one by one.
    const reusable = unusedSession(state, opts);
    if (reusable) {
      // Switching is the visible feedback: if the blank lives on another tab, "+" takes
      // you there. When it IS the current tab the call is a genuine no-op, and the
      // callers disable their button so it never looks like a dead tap.
      setState((current) =>
        current.currentSessionId === reusable.id
          ? current
          : {
              ...current,
              currentSessionId: reusable.id,
              activeSessions: ensureActiveSessions(current, reusable.id),
            },
      );
      return reusable;
    }
    const session = createSession(opts);
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

  captureHydrationEligibility(sessionId: string): HydrationEligibility | null {
    if (locallyDeletedIds.has(sessionId)) return null;
    const localSession = state.sessions.find((session) => session.id === sessionId) ?? null;
    if (localSession?.messages.length) return null;
    return { sessionId, localSession };
  },

  hydrateSessions(sessions: ChatSession[], eligibility: HydrationEligibility[] = []) {
    const tokens = new Map(eligibility.map((token) => [token.sessionId, token.localSession]));
    setState((current) => {
      const eligible = sessions.filter((session) => {
        if (locallyDeletedIds.has(session.id)) return false;
        if (!tokens.has(session.id)) return eligibility.length === 0;
        const now = current.sessions.find((candidate) => candidate.id === session.id) ?? null;
        return now === tokens.get(session.id) && !now?.messages.length;
      });
      return mergeHydratedSessions(current, eligible);
    });
  },

  deleteSession(sessionId: string) {
    locallyDeletedIds.add(sessionId); // tombstone: the cross-tab merge won't resurrect it
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

  /** Ask for a session to be deleted THROUGH the confirm lifecycle rather than
   *  deleting it here. ChatSurface consumes the request into its existing
   *  pendingClose dialog (one consumer at a time — it waits out an open dialog). */
  requestDeleteSession(sessionId: string) {
    setState((current) =>
      current.pendingDeleteRequest === sessionId
        ? current
        : { ...current, pendingDeleteRequest: sessionId },
    );
  },

  clearDeleteRequest() {
    setState((current) =>
      current.pendingDeleteRequest === null ? current : { ...current, pendingDeleteRequest: null },
    );
  },

  /** Ask for a session's history to be CLEARED (wiped, tab kept) through the confirm
   *  lifecycle rather than wiping it here. The ⌘K keybinding and the /clear slash command
   *  run outside React, so they park the id here; ChatSurface consumes it into a
   *  "Clear this conversation?" dialog (harvest opt-in) and only then wipes (#2996). */
  requestClearSession(sessionId: string) {
    setState((current) =>
      current.pendingClearRequest === sessionId
        ? current
        : { ...current, pendingClearRequest: sessionId },
    );
  },

  clearClearRequest() {
    setState((current) =>
      current.pendingClearRequest === null ? current : { ...current, pendingClearRequest: null },
    );
  },

  switchSession(sessionId: string) {
    setState((current) => ({
      ...current,
      currentSessionId: sessionId,
      activeSessions: ensureActiveSessions(current, sessionId),
    }));
  },

  /** Reorder the session tabs to match `orderedIds` (from the DS TabBar's
   *  `onReorder`, ui@0.43.0). Pure reordering — active session + status untouched;
   *  persists immediately so the order survives reload. */
  reorderSessions(orderedIds: string[]) {
    setState((current) => {
      const byId = new Map(current.sessions.map((s) => [s.id, s]));
      const next = orderedIds
        .map((id) => byId.get(id))
        .filter((s): s is ChatSession => s != null);
      // Defensive: keep any session the caller omitted, in its original order.
      if (next.length !== current.sessions.length) {
        for (const s of current.sessions) {
          if (!orderedIds.includes(s.id)) next.push(s);
        }
      }
      return { ...current, sessions: next };
    });
  },

  updateMessages(sessionId: string, messages: ChatMessage[]) {
    // Fires per streamed SSE frame (~24 chars) and per reveal-queue tick
    // (~word-sized, #2993) — debounce the localStorage write. The stream-done
    // path flushes via setSessionStatus right after the final updateMessages,
    // so the terminal state always lands immediately.
    const deduped = dedupeMessages(messages);
    setState(
      (current) => ({
        ...current,
        sessions: current.sessions.map((session) =>
          session.id === sessionId
            ? {
                ...session,
                title: session.title === DEFAULT_SESSION_TITLE ? titleFromMessages(deduped) : session.title,
                messages: deduped,
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
        session.id === sessionId ? { ...session, title: title.trim() || DEFAULT_SESSION_TITLE } : session,
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

  // Per-tab reasoning effort (the /effort command). Empty string clears it (→ the
  // default level on the next turn).
  setSessionReasoningEffort(sessionId: string, effort: string) {
    setState((current) => ({
      ...current,
      sessions: current.sessions.map((session) =>
        session.id === sessionId ? { ...session, reasoningEffort: effort || undefined } : session,
      ),
    }));
  },

  // Per-tab bypass-permissions toggle (the /bypass command). Dangerous — auto-approves
  // run_command for this tab's turns until turned off.
  setSessionBypassPermissions(sessionId: string, on: boolean) {
    setState((current) => ({
      ...current,
      sessions: current.sessions.map((session) =>
        session.id === sessionId ? { ...session, bypassPermissions: on || undefined } : session,
      ),
    }));
  },

  // Per-tab incognito toggle (ADR 0069 D3b). Persisted with the session so the mode
  // survives reload — every send while ON carries metadata.incognito.
  setSessionIncognito(sessionId: string, on: boolean) {
    setState((current) => ({
      ...current,
      sessions: current.sessions.map((session) =>
        session.id === sessionId ? { ...session, incognito: on || undefined } : session,
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
