import { useSyncExternalStore } from "react";

import type { ChatMessage, FormResponseDisplay, FormResponseField, FormResponseGroup } from "../lib/types";
import { rendersText, textRuns } from "./parts";
import {
  applyCanonicalTurnText,
  repairAddressedTurnEcho,
  repairDuplicatedTurnText,
  shownRuns,
  turnBubbleIndexes,
} from "./turnText";
import { entryBytes, isQuotaError, listKeys, matchKey, readKey, removeKey, storagePressure, writeKey } from "../lib/storage";
import {
  evict,
  fleetSlugsSnapshot,
  LOCAL_BUDGET_BYTES,
  LOCAL_TARGET_BYTES,
  reportEviction,
  subscribeEvictions,
  type EvictionReport,
} from "../lib/storageEviction";
import { agentChunkPrefix, expandSessions, orphanChunks, packSessions } from "./transcriptChunks";
import { openTranscriptStore, type SessionLike, type TranscriptStore } from "./transcriptStore";
import { attachmentDisplay, validThumbnails } from "./imageThumbnails";
import { persistBlocked } from "../lib/storageReset";

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
  // The server's durable turn count (`/api/chat/sessions` `turn_count`) this copy was last
  // reconciled against at boot. A later count above it — and above the turns this copy
  // already holds — means another client added turns this cache never saw, so boot
  // hydration reads the session's turns again (sessionHydration.isBehindServer). Recorded
  // even when that read added nothing, so a count this copy can never match (a turn
  // regenerated away, a tail past the read window) is not re-fetched on every boot.
  serverTurnCount?: number;
  // The server's newest turn (`/api/chat/sessions` `last_task_id`) this copy was last
  // reconciled against. A newest turn this copy doesn't hold — and hasn't already
  // reconciled against — means another client ran a turn here; unlike the count, it
  // still says so once this copy remembers turns the store has swept (24h retention).
  serverLastTaskId?: string;
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

export type ServerTurnControlState = {
  sessionId: string;
  taskId: string;
  origin: string;
  trigger: string;
  controllable: boolean;
  operatorControllable: boolean;
};

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
  // bd-v92b server-initiated-turn control affordance. Ephemeral: the backend owns the
  // durable task and republishes control frames while the turn is live; persisting this
  // would leave stale Stop/interjection controls after a completed turn.
  serverTurnControls: Record<string, ServerTurnControlState>;
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

// Non-destructive tab dismissal is distinct from server retirement. Goal detach
// intentionally leaves its durable session running, so remember that local UI
// choice across reloads instead of letting boot hydration reopen the tab.
const DISMISSED_STORAGE_KEY = `${STORAGE_KEY}.dismissed`;

function loadDismissedIds(): Set<string> {
  try {
    const parsed = JSON.parse(readKey("local", DISMISSED_STORAGE_KEY) || "[]");
    return new Set(Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === "string") : []);
  } catch {
    return new Set();
  }
}

function persistDismissedIds(ids: Set<string>) {
  try {
    writeKey("local", DISMISSED_STORAGE_KEY, JSON.stringify([...ids].sort()));
  } catch {
    // Hardened storage contexts keep the in-memory dismissal for this page.
  }
}

const locallyDismissedIds = loadDismissedIds();

// Sessions this tab deleted — a lightweight per-tab tombstone so the cross-tab merge
// never resurrects a chat we just removed from another tab's stale on-disk copy.
const locallyDeletedIds = new Set<string>(locallyDismissedIds);

/** Identity token captured immediately before a durable turn read. Any local
 * edit replaces the session object, so the exact reference is a cheap per-tab
 * generation: delete, clear, send, rename, or settings changes while the read
 * is in flight veto the stale recovery result. */
export type HydrationEligibility = {
  sessionId: string;
  localSession: ChatSession | null;
  /** A non-empty local copy the server has turns beyond (another client added them):
   *  reconciled through `reconcileSessions`, not filled through `hydrateSessions`. */
  behind?: boolean;
};

/** A boot reconcile of a non-empty local session that fell behind the server: the
 *  transcript to adopt (`messages`, or null when there was nothing to add) and the server
 *  count it now answers for. Applied only while the session is still `token.localSession`. */
export type SessionReconcile = {
  token: HydrationEligibility;
  messages: ChatMessage[] | null;
  serverTurnCount: number;
  serverLastTaskId?: string | null;
};

function hydrationCanRepairMessage(message: ChatMessage | undefined): message is ChatMessage {
  if (!message || message.role !== "assistant" || message.status === "streaming") return false;
  if (!message.parts?.length) return false;
  return Boolean(message.taskId && (message.toolCalls?.length || message.components?.length));
}

/** Would durable hydration have prose to reconcile into THIS assistant message?
 *  A completed, parts-bearing tool/component reply whose ordered parts don't carry
 *  its flat `content` answer renders the cards but no text — the #3340 regression.
 *  Judged per-message so a whole-session scan can still catch an earlier stale turn.
 *
 *  "Carry" is `rendersText` — the same test the terminal replace uses. A byte compare
 *  flagged EVERY healthy narrate → tool → narrate turn: `content` keeps the server's
 *  paragraph break between model calls, the parts drop it at the run boundary, so
 *  such sessions were re-downloaded and rewritten on every boot. */
function messageNeedsDurableHydration(message: ChatMessage): boolean {
  if (!hydrationCanRepairMessage(message)) return false;
  // An `@`-addressed turn has no prose of its own to be missing (#3449): its answer was
  // spoken by the participants, whose bubbles carry it, and the durable answer text only
  // restates them. Left in, this is the shape hydration most wants to "repair" — a
  // settled, task-bearing bubble holding the work card and no text — and the repair would
  // land the whole answer a second time on every boot, permanently. `repairHydratedMessages`
  // refuses it too (via `applyCanonicalTurnText`); this stops the session being re-fetched
  // and rewritten for nothing.
  if (message.answeredByParticipants) return false;
  const runs = textRuns(message.parts);
  if (!runs.some((run) => run.trim())) return true;
  return Boolean(message.content.trim()) && !rendersText(runs, message.content);
}

/** True when ANY assistant turn in the session is a settled tool/component reply
 *  missing its rendered prose. Scanning every message — not just the last assistant
 *  (#3340 review) — is load-bearing: a healthy later reply must not mask an earlier
 *  stale turn, which would otherwise stay permanently stripped of its answer text. */
export function needsDurableHydration(session: ChatSession): boolean {
  const lastTurn = lastTurnOfTask(session.messages);
  return session.messages.some(
    (message) =>
      messageNeedsDurableHydration(message) &&
      // An earlier turn of a task a HITL answer continued has no prose of its own to be
      // missing — the reply belongs to the turn after the answer (see lastTurnOfTask).
      lastTurn.get(message.taskId as string) === (message.splitOf ?? message.id),
  );
}

/** Each task's LAST local turn, as taskId → the turn's anchor id (a split bubble's
 *  `splitOf`, else its own id). A HITL answer (a form, an `ask_human` reply) continues the
 *  task that parked (#3930) as a NEW rendered turn — the question bubble, the operator's
 *  answer, then the reply bubble, all on one taskId and linked by no `splitOf`. The task's
 *  recovered answer is the reply, which the turn after the operator's answer owns; writing
 *  it onto the question bubble too rendered the reply twice after a reload. */
function lastTurnOfTask(messages: ChatMessage[]): Map<string, string | undefined> {
  const last = new Map<string, string | undefined>();
  for (const message of messages) {
    if (message.role !== "assistant" || !message.taskId || message.author) continue;
    last.set(message.taskId, message.splitOf ?? message.id);
  }
  return last;
}

function repairHydratedMessages(local: ChatMessage[], recovered: ChatMessage[]): ChatMessage[] | null {
  const recoveredByTask = new Map<string, ChatMessage>();
  for (const message of recovered) {
    if (message.role === "assistant" && message.status !== "streaming" && message.taskId && message.content.trim()) {
      recoveredByTask.set(message.taskId, message);
    }
  }
  if (!recoveredByTask.size) return null;
  // A recovered turn is the WHOLE answer, and one local turn can render as SEVERAL
  // bubbles sharing its taskId once a consumed steer / delegation split it. Anchor
  // the repair on each turn's trailing bubble and DISTRIBUTE the recovered text
  // across the turn (turnText.ts) — writing it onto every bubble that matches the
  // taskId, which this repair used to do, renders the earlier bubbles' prose twice.
  const turns: { anchorId: string; taskId: string }[] = [];
  const seen = new Set<string>();
  // Only a task's LAST local turn takes its recovered answer (see lastTurnOfTask).
  const lastTurn = lastTurnOfTask(local);
  for (const message of local) {
    if (message.role !== "assistant" || !message.taskId || !message.id || message.author) continue;
    const anchorId = message.splitOf ?? message.id; // the turn's continuation owns it
    if (seen.has(anchorId)) continue;
    seen.add(anchorId);
    turns.push({ anchorId, taskId: message.taskId });
  }
  let messages = local;
  let changed = false;
  for (const { anchorId, taskId } of turns) {
    if (lastTurn.get(taskId) !== anchorId) continue;
    const recoveredMessage = recoveredByTask.get(taskId);
    if (!recoveredMessage) continue;
    const indexes = turnBubbleIndexes(messages, anchorId);
    if (!indexes.some((index) => hydrationCanRepairMessage(messages[index]))) continue;
    if (indexes.some((index) => messages[index].status === "streaming")) continue;
    // Already rendered (the same test as the terminal replace): leave the turn — and
    // the session object — untouched, so a healthy session is never rewritten.
    const shown = indexes.flatMap((index) => shownRuns(messages[index]));
    if (rendersText(shown, recoveredMessage.content)) continue;
    messages = applyCanonicalTurnText(messages, anchorId, recoveredMessage.content);
    changed = true;
  }
  return changed ? messages : null;
}

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

/** Whether `sessionId` has a kickoff queued (the slot's gate reads it before deciding). */
export function hasGoalKickoff(sessionId: string): boolean {
  return pendingGoalKickoffs.has(sessionId);
}

function id(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** The title a session carries until its first user message renames it (or the operator
 *  does). Load-bearing beyond display: `unusedSession` treats it as "never used", and the
 *  auto-title on first reply only fires while the title is still this. */
export const DEFAULT_SESSION_TITLE = "New chat";

function titleFromMessages(messages: ChatMessage[]) {
  const first = messages.find((message) => message.role === "user");
  const text = first?.content.trim() || attachmentDisplay("", first?.attachments?.map((a) => a.name) ?? []);
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
    // A duplicate entry that made it into a persisted blob (#1938) collapses on load,
    // and a turn whose split bubbles were each handed the whole canonical answer —
    // written before the fix, so `dedupeMessages` never saw colliding ids — has its
    // duplicated prose stripped back to one copy (turnText.ts).
    .map((s) => {
      // Two one-time repairs, both self-limiting: a split turn handed the whole
      // canonical answer (#3387), and an ADDRESSED turn whose answer was drawn once
      // under the participant's byline and once again as the lead's (#3449 — the
      // shape v0.164.0 persisted, which nothing else heals). `normalizeFormResponses`
      // additionally drops a corrupt HITL form-response label map (#4259) so it can't
      // reach the card renderer; all return the same array when nothing changed.
      const messages = repairAddressedTurnEcho(
        repairDuplicatedTurnText(dedupeMessages(normalizeFormResponses(s.messages))),
      );
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

/** A session whose live turn is parked on the operator: its lead bubble is still open
 *  (`streaming`) and marked paused (an `ask_human` question, a form, an approval). */
export function isParkedSession(session: ChatSession): boolean {
  const last = [...session.messages].reverse().find((message) => message.role === "assistant");
  return Boolean(last?.status === "streaming" && last.paused && last.taskId);
}

/** Fold server-recovered sessions into the local-first store (#2888).
 * Non-empty local transcripts win here because they carry richer ordered
 * parts and client-only annotations — a local copy the server has turns BEYOND
 * goes through `reconcileSessions` instead (sessionHydration.isBehindServer). A locally empty copy may be recovered,
 * and server-only sessions fill only the remaining cap (parked ones first, #3957). The sole auto-created
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
    if (existing?.messages.length) {
      const messages = repairHydratedMessages(existing.messages, recovered.messages);
      if (!messages) continue;
      byId.set(recovered.id, { ...existing, messages, updatedAt: Math.max(existing.updatedAt, recovered.updatedAt) });
      hydratedIds.add(recovered.id);
      continue;
    }
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
  // The newest fill the free slots — but a session PARKED on the operator (its question
  // or approval still waiting) is kept first, however old: dropping it here would lose the
  // one tab that still needs the operator (#3957). Drawn oldest-first either way.
  const kept = new Set(
    [...missing.filter(isParkedSession).reverse(), ...missing.filter((session) => !isParkedSession(session)).reverse()]
      .slice(0, slots)
      .map((session) => session.id),
  );
  const additions = missing.filter((session) => kept.has(session.id));
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
    // A parked (paused) turn is not producing — same rule as bootLiveTurns (#3956).
    if (last?.status === "streaming" && last.taskId && !last.paused) sessionStatusMap[session.id] = "streaming";
    else if (last?.status === "error") sessionStatusMap[session.id] = "error";
  }
  return { ...current, sessions, currentSessionId, activeSessions, sessionStatusMap };
}

/** Validate a persisted `formResponse` (#4259) from a possibly hand-edited / older blob: keep a
 *  well-formed display (optional string title + groups of string label/value pairs) and return
 *  `undefined` for anything else, so a corrupt entry degrades to the message's legacy text
 *  instead of rendering a broken card or throwing past the panel boundary (same defensive stance
 *  as `isValidSession`). Presentation-only — it never touches `content`, the resume value. */
export function sanitizeFormResponse(value: unknown): FormResponseDisplay | undefined {
  if (!value || typeof value !== "object") return undefined;
  const d = value as { title?: unknown; groups?: unknown };
  if (!Array.isArray(d.groups)) return undefined;
  const groups: FormResponseGroup[] = [];
  for (const g of d.groups) {
    const rawFields = (g as { fields?: unknown } | null)?.fields;
    if (!g || typeof g !== "object" || !Array.isArray(rawFields)) continue;
    const fields: FormResponseField[] = [];
    for (const f of rawFields) {
      const field = f as { label?: unknown; value?: unknown };
      if (f && typeof f === "object" && typeof field.label === "string" && typeof field.value === "string") {
        fields.push({ label: field.label, value: field.value });
      }
    }
    if (!fields.length) continue;
    const title = (g as { title?: unknown }).title;
    groups.push(typeof title === "string" ? { title, fields } : { fields });
  }
  if (!groups.length) return undefined;
  return typeof d.title === "string" ? { title: d.title, groups } : { groups };
}

/** Drop a structurally-invalid `formResponse` off any message that carries one (#4259),
 *  returning the SAME array when every message is already clean so a healthy session is never
 *  rewritten on load. A valid display is kept as-is (it already round-trips as JSON). */
function normalizeFormResponses(messages: ChatMessage[]): ChatMessage[] {
  if (!messages.some((m) => m.formResponse !== undefined)) return messages;
  let changed = false;
  const next = messages.map((m) => {
    if (m.formResponse === undefined) return m;
    if (sanitizeFormResponse(m.formResponse)) return m; // well-formed — keep the original object
    changed = true;
    const { formResponse: _dropped, ...rest } = m;
    return rest;
  });
  return changed ? next : messages;
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

// ── chunked transcripts (#4973, chat/transcriptChunks.ts) ─────────────────────
// A chunk key is named by its content, so a parsed chunk never goes stale: cache it, and
// a save that re-reads the main key doesn't re-parse a chat's whole settled history.
const chunkCache = new Map<string, ChatMessage[]>();

function readChunk(key: string): ChatMessage[] | null {
  const cached = chunkCache.get(key);
  if (cached) return cached;
  try {
    const raw = readKey("local", key);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    if (!Array.isArray(parsed)) return null;
    chunkCache.set(key, parsed as ChatMessage[]);
    return parsed as ChatMessage[];
  } catch {
    return null;
  }
}

/** A main-key value as the in-memory state: every chat's chunks put back before its tail. */
export function parsePersisted(raw: string | null): PersistedChatState | null {
  if (!raw) return null;
  return sanitizePersisted(expandSessions(STORAGE_KEY, JSON.parse(raw), readChunk));
}

/** Write `payload` chunked: new chunks first, then the main key that names them, then drop
 *  this agent's chunks nothing names any more. A chunk that can't be written makes this
 *  save fall back to the old inline form (the caller's quota path). Returns the main write. */
function writeChunked(payload: PersistedChatState): ReturnType<typeof writeKey> | null {
  const { sessions, chunkValues } = packSessions(STORAGE_KEY, payload.sessions);
  const prefix = agentChunkPrefix(STORAGE_KEY);
  const present = new Set(listKeys("local").filter((key) => key.startsWith(prefix)));
  for (const [key, json] of chunkValues) {
    if (present.has(key)) continue;
    if (!writeKey("local", key, json).ok) return null;
    present.add(key);
    chunkCache.set(key, JSON.parse(json) as ChatMessage[]);
  }
  const result = writeKey("local", STORAGE_KEY, JSON.stringify({ ...payload, sessions }));
  if (result.ok) collectChunks(present, new Set(chunkValues.keys()));
  return result;
}

function collectChunks(present: Iterable<string>, referenced: ReadonlySet<string>) {
  for (const key of orphanChunks(STORAGE_KEY, present, referenced)) {
    removeKey("local", key);
    chunkCache.delete(key);
  }
}

// ── IndexedDB backend (epic #4981 S2, chat/transcriptStore.ts) ───────────────
// WebKit's localStorage stops checkpointing its write-ahead log after the first overwrite of a
// value over 1K characters (#4974), and every transcript save was one. So transcripts live in
// IndexedDB once `initTranscriptBackend` has opened it — awaited in main.tsx BEFORE React
// mounts, so no surface can read or mutate a session that hasn't loaded yet (the boot gate that
// replaces ADR 0114 D2's per-session load barrier). If IndexedDB is missing, refuses, or stalls,
// the console keeps the localStorage path above for the page's lifetime.
//
// Writes are per session: only sessions whose object changed since the last save are written
// (state updates are immutable, so a reference compare is exact), so one tab's save can't
// clobber another tab's other chats. Saves are chained so they commit in order. Other tabs hear
// about them on a BroadcastChannel and re-read just those sessions.

/** This window's agent in the transcript store ("host" for the bare key). */
const AGENT = STORAGE_KEY.includes(":") ? STORAGE_KEY.slice(STORAGE_KEY.indexOf(":") + 1) : "host";
export const TRANSCRIPT_CHANNEL = "protoagent:transcripts";

let transcripts: TranscriptStore | null = null;
/** What the store holds for this agent, by the session OBJECT last written. */
const persistedRefs = new Map<string, ChatSession>();
let writeChain: Promise<void> = Promise.resolve();
let transcriptChannel: BroadcastChannel | null = null;
const transcriptListeners = new Set<(agent: string) => void>();
/** The tab order + focused chat last saved to the store's `agents` record. */
let lastAgentWritten = "";
/** Deletes whose write failed: retried with the next save. */
const pendingRemovals = new Set<string>();

type TranscriptMessage = { agent: string; ids: string[]; deleted: string[]; evicted?: string[] };

/** The session as it is saved: ephemeral overlays dropped, only bounded image previews kept. */
function storable(session: ChatSession): ChatSession {
  const messages = session.messages
    .filter((m) => !m.ephemeral)
    .map((m) => (m.attachments ? { ...m, attachments: validThumbnails(m.attachments) } : m));
  return { ...session, messages };
}

function persistToTranscripts(current: ChatState) {
  const store = transcripts!;
  const sessions = current.sessions.slice(0, MAX_SESSIONS);
  const changed = sessions.filter((session) => persistedRefs.get(session.id) !== session);
  const live = new Set(sessions.map((session) => session.id));
  const removed = [...new Set([...persistedRefs.keys(), ...pendingRemovals])].filter((id) => !live.has(id));
  pendingRemovals.clear();
  for (const session of changed) persistedRefs.set(session.id, session);
  for (const id of removed) persistedRefs.delete(id);
  const agentRecord = { slug: AGENT, order: sessions.map((s) => s.id), currentSessionId: current.currentSessionId };
  const agentKey = JSON.stringify(agentRecord);
  const agentChanged = agentKey !== lastAgentWritten;
  lastAgentWritten = agentKey;
  if (!changed.length && !removed.length && !agentChanged) return;
  const payload = changed.map((session) => storable(session) as unknown as SessionLike);
  writeChain = writeChain
    .then(async () => {
      await withQuotaEviction(() => store.putSessions(AGENT, payload));
      await store.deleteSessions(AGENT, removed);
      if (agentChanged) await store.putAgent(agentRecord);
      transcriptChannel?.postMessage({ agent: AGENT, ids: changed.map((s) => s.id), deleted: removed } satisfies TranscriptMessage);
    })
    .catch((err) => {
      // A failed save (quota: S4) must not stop later ones. The next change re-saves the
      // session, so forget the reference that never made it.
      for (const session of changed) if (persistedRefs.get(session.id) === session) persistedRefs.delete(session.id);
      // …and redo the tab order and the deletes too: none of them is known to have landed.
      if (agentChanged) lastAgentWritten = "";
      for (const id of removed) pendingRemovals.add(id);
      console.error("[chat] saving to IndexedDB failed", err);
    });
}

/** The palette thread key prefix of this window (mirrors app/paletteChatStore.ts). */
const PALETTE_KEY = AGENT === "host" ? "protoagent.palette.chat" : `protoagent.palette.chat:${AGENT}`;

/** Run an IndexedDB write; on a quota error free space (ADR 0114 D5) one step at a time and
 *  retry, until it fits or nothing is left to remove. Never removes this agent's open tabs or
 *  a chat with a live turn. Says what it cleared (the storage toast). */
export async function withQuotaEviction(
  write: () => Promise<void>,
  source: SaveSource | null = "chats",
): Promise<void> {
  const failing = (on: boolean) => {
    if (source) setSaveFailing(source, on);
  };
  try {
    await write();
    failing(false);
    return;
  } catch (err) {
    if (!isQuotaError(err) || !transcripts) throw err;
  }
  const protect = protectedChatIds();
  const all: { slug: string; id: string; bytes: number }[] = [];
  // One toast for the whole squeeze, and other tabs told what went (they may hold it open).
  const announce = () => {
    if (!all.length) return;
    reportEviction({
      reason: evictionReason,
      freedBytes: all.reduce((n, r) => n + r.bytes, 0),
      evicted: all.map((r) => ({ key: `idb:${r.slug}/${r.id}`, slug: r.slug, bytes: r.bytes })),
    });
    const bySlug = new Map<string, string[]>();
    for (const r of all) if (r.slug !== "palette") bySlug.set(r.slug, [...(bySlug.get(r.slug) ?? []), r.id]);
    for (const [agent, ids] of bySlug) {
      transcriptChannel?.postMessage({ agent, ids: [], deleted: [], evicted: ids } satisfies TranscriptMessage);
    }
  };
  try {
    for (let step = 0; step < 50; step++) {
      const removed = await transcripts.evictStep({ keepSlug: AGENT, fleet: fleetSlugsSnapshot(), protect, threadPrefix: PALETTE_KEY });
      if (!removed.length) break;
      all.push(...removed);
      try {
        await write();
        failing(false);
        return;
      } catch (err) {
        if (!isQuotaError(err)) throw err;
      }
    }
    try {
      await write(); // nothing left to free: let the quota error surface to the caller's catch
      failing(false);
    } catch (err) {
      if (isQuotaError(err)) failing(true);
      throw err;
    }
  } finally {
    // This agent's evicted chats aren't open here (they were never `protect`ed): drop them from
    // this tab too, or its next save would write them straight back. A local drop, not a delete.
    for (const r of all) {
      if (r.slug !== AGENT) continue;
      persistedRefs.delete(r.id);
      if (state.sessions.some((s) => s.id === r.id) && state.sessions.length > 1) removeLocalSession(r.id);
    }
    announce();
  }
}

// ── Settings → Storage (docs/design/settings-storage.md) ─────────────────────
// What the page needs from the chat store: whether IndexedDB saves are failing (the D6
// `failing` state; the localStorage latch is storage.ts's), which of this agent's chats are in
// use, clearing one agent's saved chats from this browser, and Free up space.

/** Chats of this agent that clearing never removes: open tabs, the focused chat, live replies. */
export function protectedChatIds(): Set<string> {
  return new Set([...state.activeSessions, ...streamingIds(state), ...(state.currentSessionId ? [state.currentSessionId] : [])]);
}

/** This window's agent as the transcript store names it ("host" for this instance). */
export function transcriptAgent(): string {
  return AGENT;
}

/** Which saves are failing for lack of space. Chats and quick chats are tracked apart, so a quick
 *  chat's save succeeding never hides that chat saves still fail (and the other way round). */
export type SaveSource = "chats" | "palette";
const failingSources = new Set<SaveSource>();
let saveFailing = false;
const saveFailingListeners = new Set<() => void>();
/** How a source re-runs its latest failed save (the palette registers one): Free up space
 *  calls it once it has made room, so that source's failing state can clear without waiting
 *  for the operator's next message. */
const saveRetries = new Map<SaveSource, () => Promise<void>>();
export function registerSaveRetry(source: SaveSource, retry: () => Promise<void>): void {
  saveRetries.set(source, retry);
}
/** Who an IndexedDB eviction is reported as: Free up space runs the same routine by hand. */
let evictionReason: EvictionReport["reason"] = "quota";

function setSaveFailing(source: SaveSource, on: boolean) {
  if (on) failingSources.add(source);
  else failingSources.delete(source);
  const next = failingSources.size > 0;
  if (saveFailing === next) return;
  saveFailing = next;
  for (const fn of saveFailingListeners) {
    try {
      fn();
    } catch {
      /* a listener never breaks a save */
    }
  }
}

/** True while an IndexedDB save is failing for lack of space, even after clearing old chats. */
export function transcriptSaveFailing(): boolean {
  return saveFailing;
}

export function subscribeTranscriptSaveFailing(fn: () => void): () => void {
  saveFailingListeners.add(fn);
  return () => saveFailingListeners.delete(fn);
}

/** Clear one agent's saved chats from THIS browser (never the server's copy). For this window's
 *  agent, chats open in a tab, the focused chat and a live reply stay, as with automatic
 *  clearing. Other tabs of that agent drop their copies of the cleared chats unless they have
 *  them open. Not an automatic clear: nothing is reported as an eviction. Returns what went. */
export async function clearSavedChats(slug: string): Promise<{ chats: number; bytes: number }> {
  const protect = slug === AGENT ? protectedChatIds() : new Set<string>();
  if (transcripts) {
    await writeChain; // let a queued save land first, so it can't write a cleared chat back
    const { sessions } = await transcripts.usage();
    const gone = sessions.filter((s) => s.slug === slug && !protect.has(s.id));
    const ids = gone.map((s) => s.id);
    await transcripts.deleteSessions(slug, ids);
    if (slug === AGENT) dropLocalCopies(ids);
    if (ids.length) transcriptChannel?.postMessage({ agent: slug, ids: [], deleted: [], evicted: ids } satisfies TranscriptMessage);
    return { chats: ids.length, bytes: gone.reduce((n, s) => n + s.bytes, 0) };
  }
  // localStorage: the agent's key and its chunk keys.
  const key = slug === "host" ? "protoagent.chat.sessions" : `protoagent.chat.sessions:${slug}`;
  const chunkKeys = listKeys("local").filter((k) => k.startsWith(agentChunkPrefix(key)));
  if (slug !== AGENT) {
    const raw = readKey("local", key);
    let chats = 0;
    try {
      const parsed = JSON.parse(raw ?? "null") as { sessions?: unknown[] } | null;
      chats = Array.isArray(parsed?.sessions) ? parsed.sessions.length : 0;
    } catch {
      chats = 0;
    }
    const bytes = chunkKeys.reduce((n, k) => n + entryBytes(k, readKey("local", k) ?? ""), raw === null ? 0 : entryBytes(key, raw));
    removeKey("local", key);
    for (const k of chunkKeys) removeKey("local", k);
    return { chats, bytes };
  }
  let onDisk: ChatSession[] = [];
  try {
    onDisk = parsePersisted(readKey("local", STORAGE_KEY))?.sessions ?? [];
  } catch {
    onDisk = [];
  }
  const gone = onDisk.filter((s) => !protect.has(s.id));
  // The key goes first, so the save below can't merge the cleared chats back from it.
  removeKey("local", STORAGE_KEY);
  for (const k of chunkKeys) removeKey("local", k);
  dropLocalCopies(gone.map((s) => s.id));
  persistDirty = true;
  flushChatPersist();
  return { chats: gone.length, bytes: gone.reduce((n, s) => n + 2 * JSON.stringify(s).length, 0) };
}

/** This tab's copies of cleared chats go (a local drop, not a retirement: the server's 24 h
 *  history can still bring one back). */
function dropLocalCopies(ids: string[]) {
  const inUse = protectedChatIds();
  for (const id of ids) {
    persistedRefs.delete(id);
    if (!inUse.has(id) && state.sessions.some((s) => s.id === id) && state.sessions.length > 1) removeLocalSession(id);
  }
}

/** Free up space (Settings → Storage, in the `failing` state): the automatic routine started by
 *  hand. It clears old chats in the D5 order until this agent's unsaved chats fit, reports it as
 *  an automatic clear (reason `free`, so the "Last automatic clear" record is written), and says
 *  what it removed and whether saving works again. */
export async function freeUpSpace(): Promise<{ chats: number; bytes: number; saving: boolean }> {
  let chats = 0;
  let bytes = 0;
  const off = subscribeEvictions((report) => {
    if (report.reason !== "free") return;
    chats += report.evicted.reduce((n, e) => n + (e.chats ?? 1), 0);
    bytes += report.freedBytes || report.evicted.reduce((n, e) => n + e.bytes, 0);
  });
  evictionReason = "free";
  try {
    if (storagePressure().state === "failing") {
      evict(LOCAL_BUDGET_BYTES - LOCAL_TARGET_BYTES, { reason: "free" });
      persistDirty = true;
      flushChatPersist();
    }
    if (transcripts) {
      // The chats whose save failed are written again; a quota error on the way clears old
      // chats one step at a time (withQuotaEviction) until they fit.
      const before = writeChain;
      persistToTranscripts(state);
      const attempted = writeChain !== before;
      await writeChain;
      if (!attempted && saveFailing) {
        // Nothing of this agent's is waiting (a quick chat's save failed): clear one step. A
        // probe, not a save: it tracks no failing state, so only the next REAL save that fits
        // can say saving works again.
        let first = true;
        await withQuotaEviction(async () => {
          if (!first) return;
          first = false;
          throw new DOMException("The quota has been exceeded.", "QuotaExceededError");
        }, null).catch(() => undefined);
      }
      // Re-run the other sources' failed saves now there may be room (a quick chat's).
      for (const source of [...failingSources]) {
        if (source === "chats") continue;
        await saveRetries.get(source)?.().catch(() => undefined);
      }
    }
  } finally {
    evictionReason = "quota";
    off();
  }
  return { chats, bytes, saving: !saveFailing && storagePressure().state !== "failing" };
}

/** The open transcript store, or `null` on the localStorage path (palette threads, S3). */
export function transcriptStoreIfOpen(): TranscriptStore | null {
  return transcripts;
}

/** Resolves when every save queued so far has committed (tests; a crash reset before reload). */
export function transcriptWritesSettled(): Promise<void> {
  return writeChain;
}

/** Hear that some agent's saved transcripts changed in another tab or window (FleetTurnWatch). */
export function subscribeTranscriptChanges(fn: (agent: string) => void): () => void {
  transcriptListeners.add(fn);
  return () => transcriptListeners.delete(fn);
}

/** Turns still streaming in OTHER agents' saved chats, or `null` on the localStorage path. */
export async function liveTurnsElsewhere(exceptAgent: string) {
  return transcripts ? transcripts.liveTurns(exceptAgent) : null;
}

/** Clear every saved transcript (AppCrash's reset, a backend switch). Waits for any queued save,
 *  then clears on the store's own connection, so nothing queued can land after the clear. */
export async function clearSavedTranscripts(): Promise<void> {
  await writeChain;
  const store = transcripts ?? (await openTranscriptStore());
  if (!store) return;
  await store.clear();
  persistedRefs.clear();
  if (!transcripts) store.close();
}

// AppCrash (the root boundary's fallback) must not import this module, which may be what threw:
// it reaches the queue-aware clear through this global, or clears directly when it's absent.
try {
  (globalThis as { __protoagentClearSavedTranscripts?: () => Promise<void> }).__protoagentClearSavedTranscripts =
    clearSavedTranscripts;
} catch {
  /* non-browser context */
}

async function onTranscriptMessage(message: TranscriptMessage) {
  if (!message || typeof message.agent !== "string") return;
  for (const fn of transcriptListeners) {
    try {
      fn(message.agent);
    } catch {
      /* a listener never breaks sync */
    }
  }
  if (message.agent !== AGENT || !transcripts) return;
  // Another tab evicted some of this agent's chats under quota pressure. One this tab has open
  // (or streaming) stays, and is saved again on its next change; any other copy goes quietly:
  // a local drop, not a retirement, so the server's history can still bring it back.
  if (Array.isArray(message.evicted) && message.evicted.length) {
    const inUse = new Set([...state.activeSessions, ...streamingIds(state), ...(state.currentSessionId ? [state.currentSessionId] : [])]);
    for (const id of message.evicted) {
      persistedRefs.delete(id);
      if (!inUse.has(id) && state.sessions.some((s) => s.id === id) && state.sessions.length > 1) removeLocalSession(id);
    }
  }
  if (!Array.isArray(message.ids) || !message.ids.length) return;
  const loaded = await transcripts.loadSessions(AGENT, message.ids);
  const incoming = sanitizePersisted({ version: 1, sessions: loaded, currentSessionId: null });
  if (!incoming) return;
  mergeFromStorage(incoming);
  // What we adopted is exactly what the store holds: never echo it back as a change.
  for (const session of state.sessions) {
    if (incoming.sessions.some((s) => s.id === session.id) && session === incoming.sessions.find((s) => s.id === session.id)) {
      persistedRefs.set(session.id, session);
    }
  }
}

/** Every agent's transcript keys in localStorage, imported into the store, newer copy winning,
 *  then removed (with their chunk keys). Dismissal sets stay where they are. */
async function migrateLocalTranscripts(store: TranscriptStore): Promise<void> {
  for (const key of listKeys("local")) {
    const match = matchKey("local", key);
    if (match?.spec.id !== "chat.sessions") continue;
    const agent = match.slug ?? "host";
    let parsed: PersistedChatState | null = null;
    try {
      const raw = readKey("local", key);
      parsed = raw ? sanitizePersisted(expandSessions(key, JSON.parse(raw), readChunkOf)) : null;
    } catch {
      parsed = null; // corrupt: nothing to keep
    }
    if (parsed?.sessions.length) {
      const existing = new Map((await store.loadSessions(agent, parsed.sessions.map((s) => s.id))).map((s) => [s.id, s]));
      const newer = parsed.sessions.filter((s) => !existing.has(s.id) || existing.get(s.id)!.updatedAt < s.updatedAt);
      await store.putSessions(agent, newer.map((s) => storable(s) as unknown as SessionLike));
      // Keep the tab order and focus a localStorage copy had, unless the store already has its own.
      const known = await store.loadAgent(agent);
      if (!known.currentSessionId) {
        await store.putAgent({ slug: agent, order: parsed.sessions.map((s) => s.id), currentSessionId: parsed.currentSessionId });
      }
    }
    removeKey("local", key);
    for (const chunk of listKeys("local")) if (chunk.startsWith(agentChunkPrefix(key))) removeKey("local", chunk);
  }
}

function readChunkOf(key: string): ChatMessage[] | null {
  return readChunk(key);
}

/** Open the transcript store, migrate localStorage into it, and load this agent's chats.
 *  Awaited before React mounts. Returns which backend the page uses. Never throws. */
export const TRANSCRIPT_BOOT_DEADLINE_MS = 5_000;

export async function initTranscriptBackend(
  opts: { open?: typeof openTranscriptStore; deadlineMs?: number } = {},
): Promise<"idb" | "local"> {
  if (persistBlocked()) return "local";
  // Past the deadline the app mounts on localStorage, and a late load must NOT then swap the
  // state under it: every step below checks `late` before it changes anything.
  let late = false;
  const deadline = new Promise<"local">((resolve) =>
    setTimeout(() => {
      late = true;
      resolve("local");
    }, opts.deadlineMs ?? TRANSCRIPT_BOOT_DEADLINE_MS),
  );
  return Promise.race([deadline, bootTranscripts(opts.open ?? openTranscriptStore, () => late)]);
}

async function bootTranscripts(open: typeof openTranscriptStore, isLate: () => boolean): Promise<"idb" | "local"> {
  const store = await open();
  if (!store) return "local";
  if (isLate()) {
    store.close();
    return "local";
  }
  try {
    // Anything this page saved to localStorage before the store opened goes in first.
    flushChatPersist();
    await migrateLocalTranscripts(store);
    const { sessions: loaded, currentSessionId: savedCurrent } = await store.loadAgent(AGENT);
    if (isLate()) {
      // The migration may have committed: that's fine, localStorage still holds nothing it
      // removed that the store doesn't. This page stays on localStorage; the next boot loads.
      store.close();
      return "local";
    }
    const saved = sanitizePersisted({ version: 1, sessions: loaded, currentSessionId: savedCurrent });
    transcripts = store;
    if (saved) {
      const sessions = saved.sessions.filter((session) => !locallyDismissedIds.has(session.id));
      if (sessions.length) {
        const currentSessionId = sessions.some((s) => s.id === saved.currentSessionId) ? saved.currentSessionId : sessions[0].id;
        const adopted = { ...saved, sessions, currentSessionId };
        state = { ...state, ...adopted, ...bootLiveTurns(adopted) };
      }
    }
    persistedRefs.clear();
    for (const session of state.sessions) if (loaded.some((l) => l.id === session.id)) persistedRefs.set(session.id, session);
    lastAgentWritten = JSON.stringify({ slug: AGENT, order: state.sessions.slice(0, MAX_SESSIONS).map((s) => s.id), currentSessionId: state.currentSessionId });
    try {
      transcriptChannel = new BroadcastChannel(TRANSCRIPT_CHANNEL);
      transcriptChannel.onmessage = (e) => void onTranscriptMessage(e.data as TranscriptMessage);
    } catch {
      transcriptChannel = null; // no BroadcastChannel: other tabs catch up on reload
    }
    listeners.forEach((listener) => listener());
    return "idb";
  } catch (err) {
    console.error("[chat] IndexedDB transcripts unavailable; staying on localStorage", err);
    transcripts = null;
    store.close();
    return "local";
  }
}

/** Test seam: drop the IndexedDB backend (and its sync) without touching saved data. */
export function __resetTranscriptBackendForTests(): void {
  transcripts?.close();
  transcripts = null;
  persistedRefs.clear();
  transcriptChannel?.close();
  transcriptChannel = null;
  writeChain = Promise.resolve();
  lastAgentWritten = "";
}

function loadPersisted(): PersistedChatState {
  try {
    const state = parsePersisted(readKey("local", STORAGE_KEY));
    if (state) {
      const sessions = state.sessions.filter((session) => !locallyDismissedIds.has(session.id));
      if (sessions.length) {
        return {
          ...state,
          sessions,
          currentSessionId: sessions.some((session) => session.id === state.currentSessionId)
            ? state.currentSessionId
            : sessions[0].id,
        };
      }
    }
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

// Storage recovery (ADR 0114 D6): after AppCrash's "Free up space & reload" (this page) or a
// `storage-reset` broadcast (another tab), this store stops writing — see lib/storageReset.ts.
function persist(state: ChatState) {
  if (persistBlocked()) return;
  if (transcripts) {
    persistToTranscripts(state);
    return;
  }
  try {
    // Read-merge-write: a concurrent tab sharing this key may have written sessions we
    // don't have (or newer copies) since our last read. Fold them in so our write never
    // clobbers another tab's chats (the last-writer-wins data-loss bug). Our own
    // streaming sessions stay authoritative; locally-deleted ones are not resurrected.
    let sessions = state.sessions;
    try {
      const onDisk = parsePersisted(readKey("local", STORAGE_KEY));
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
    // Only bounded preview fields enter the durable cache.
    payload.sessions = payload.sessions.map((s) => ({ ...s, messages: s.messages.map((m) =>
      m.attachments ? { ...m, attachments: validThumbnails(m.attachments) } : m) }));
    const chunked = writeChunked(payload);
    if (chunked?.ok) return;
    // Inline fallback: a chunk or the main key didn't fit. Write the old one-value form (it
    // names no chunks, so the next good save collects them), then, under quota pressure,
    // discard the oldest previews while keeping transcript text.
    let result = writeKey("local", STORAGE_KEY, JSON.stringify(payload));
    if (result.ok) collectChunks(listKeys("local"), new Set());
    const oldest = [...payload.sessions].sort((a, b) => a.updatedAt - b.updatedAt);
    for (const session of oldest) {
      for (const message of session.messages) {
        if (result.ok || result.reason !== "quota") return;
        if (!message.attachments?.length) continue;
        message.content = attachmentDisplay(message.content, message.attachments.map((a) => a.name));
        delete message.attachments;
        result = writeKey("local", STORAGE_KEY, JSON.stringify(payload), { retryReduced: true });
      }
    }
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
// updates persist on a trailing ~2s timer; structural changes (session
// add/remove/rename/switch, stream start/done) and page unload flush
// immediately. Only the localStorage WRITE is deferred — the in-memory state
// and listener notify stay synchronous, so the UI streams live.
//
// Why 2s and not shorter: every write re-stringifies the agent's WHOLE transcript
// key (hundreds of KB), and WebKit (the desktop app, Safari) stores it as one SQLite
// value — any edit shifts every overflow page after it, so each save rewrites the
// whole blob into the write-ahead log. At 300ms a 60s streamed turn wrote ~120 such
// blobs (~50 MB of WAL on a 450 KB key); in the desktop app a WebKit reader pinned
// since launch keeps that WAL from ever checkpointing until the app quits (204 MB
// after 9.5h, 2026-11-09). A crash mid-stream loses at most ~2s of text, and the
// server's durable copy of the in-flight turn redraws it (ADR 0104 reattach/hydration).

export const PERSIST_DEBOUNCE_MS = 2_000;

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

/** Write any pending (debounced) state NOW. No-op when clean, so a tenant-clear + reload
 * (lib/tenant.ts) is never undone by an unload flush that had nothing pending. Exported for
 * tests + the unload hooks.
 *
 * On localStorage this write completes before it returns. On IndexedDB (epic #4981) it only
 * STARTS the save: a transaction begun in pagehide usually commits, but nothing guarantees it
 * before teardown. What's at risk is the last ≤2s of a streaming reply (the server's copy of
 * the turn redraws it on reattach, ADR 0104) or a change made in the instant before closing. */
export function flushChatPersist() {
  if (persistTimer !== null) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }
  if (persistBlocked()) {
    persistDirty = false; // the storage was just reset — nothing may be written back
    return;
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
//
// A turn PARKED on the operator (its bubble paused — an `ask_human` question, a form, an
// approval, #3956) is in flight too, and still comes back active so its reattach brings the
// form back — but it is NOT marked `streaming`: nothing is producing into it. Only
// MAX_ACTIVE_SESSIONS slots mount, and a parked session past that cap would otherwise hold
// a streaming tab dot, the rail's any-streaming dot and a retirement block until opened.
// Opening it mounts its slot, and the reattach runs off the bubble, not the status.
export function bootLiveTurns(
  persisted: PersistedChatState,
): Pick<ChatState, "activeSessions" | "sessionStatusMap"> {
  const working: string[] = [];
  const parked: string[] = [];
  for (const session of persisted.sessions) {
    const last = [...session.messages].reverse().find((m) => m.role === "assistant");
    if (last?.status !== "streaming" || !last.taskId) continue;
    (last.paused ? parked : working).push(session.id);
  }
  // Mount order: the focused tab, then turns still producing, then parked ones.
  const activeSessions = [
    ...new Set([...(persisted.currentSessionId ? [persisted.currentSessionId] : []), ...working, ...parked]),
  ].slice(0, MAX_ACTIVE_SESSIONS);
  return {
    activeSessions,
    sessionStatusMap: Object.fromEntries(working.map((id) => [id, "streaming" as SessionStatus])),
  };
}

let state: ChatState = {
  ...initial,
  ...bootLiveTurns(initial),
  pendingDeleteRequest: null,
  pendingClearRequest: null,
  serverTurnControls: {},
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

function removeLocalSession(sessionId: string) {
  setState((current) => {
    const sessions = current.sessions.filter((session) => session.id !== sessionId);
    const currentSessionId =
      current.currentSessionId === sessionId ? sessions[0]?.id || null : current.currentSessionId;
    const sessionStatusMap = { ...current.sessionStatusMap };
    const serverTurnControls = { ...current.serverTurnControls };
    delete sessionStatusMap[sessionId];
    delete serverTurnControls[sessionId];
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
      serverTurnControls,
    };
  });
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
      const incoming = parsePersisted(e.newValue);
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

  captureHydrationEligibility(sessionId: string, opts: { behind?: boolean } = {}): HydrationEligibility | null {
    if (locallyDeletedIds.has(sessionId) || locallyDismissedIds.has(sessionId)) return null;
    const localSession = state.sessions.find((session) => session.id === sessionId) ?? null;
    if (opts.behind) return localSession?.messages.length ? { sessionId, localSession, behind: true } : null;
    if (localSession?.messages.length && !needsDurableHydration(localSession)) return null;
    return { sessionId, localSession };
  },

  /** Commit boot reconciles of local sessions that fell behind the server (another
   *  client's turns). Each applies only if the session is still the exact object read
   *  before the fetch — any local edit in between (a send, a reattach settling a turn, a
   *  delete) vetoes it, and the next boot tries again. */
  reconcileSessions(updates: SessionReconcile[]) {
    if (!updates.length) return;
    setState((current) => {
      let changed = false;
      const sessionStatusMap = { ...current.sessionStatusMap };
      const sessions = current.sessions.map((session) => {
        const update = updates.find((candidate) => candidate.token.sessionId === session.id);
        if (!update || session !== update.token.localSession) return session;
        if (locallyDeletedIds.has(session.id) || locallyDismissedIds.has(session.id)) return session;
        changed = true;
        const marks = {
          serverTurnCount: update.serverTurnCount,
          ...(update.serverLastTaskId ? { serverLastTaskId: update.serverLastTaskId } : {}),
        };
        if (!update.messages) return { ...session, ...marks };
        const last = [...update.messages].reverse().find((message) => message.role === "assistant");
        // The adopted tail can end on a turn still running elsewhere: mark it like a
        // hydrated session's live turn (mergeHydratedSessions), so the slot reattaches.
        if (last?.status === "streaming" && last.taskId && !last.paused) sessionStatusMap[session.id] = "streaming";
        // A newer `updatedAt` so a sibling tab's merge (mergeSessions) takes this copy.
        return {
          ...session,
          messages: update.messages,
          ...marks,
          updatedAt: Math.max(session.updatedAt, Date.now()),
        };
      });
      return changed ? { ...current, sessions, sessionStatusMap } : current;
    });
  },

  hydrateSessions(sessions: ChatSession[], eligibility: HydrationEligibility[] = []) {
    const tokens = new Map(eligibility.map((token) => [token.sessionId, token.localSession]));
    setState((current) => {
      const eligible = sessions.filter((session) => {
        if (locallyDeletedIds.has(session.id) || locallyDismissedIds.has(session.id)) return false;
        if (!tokens.has(session.id)) return eligibility.length === 0;
        const now = current.sessions.find((candidate) => candidate.id === session.id) ?? null;
        const tokenSession = tokens.get(session.id);
        return now === tokenSession && (!now?.messages.length || (now != null && needsDurableHydration(now)));
      });
      return mergeHydratedSessions(current, eligible);
    });
  },

  deleteSession(sessionId: string) {
    // Durable retirement makes a local-only dismissal redundant.
    if (locallyDismissedIds.delete(sessionId)) persistDismissedIds(locallyDismissedIds);
    locallyDeletedIds.add(sessionId); // tombstone: the cross-tab merge won't resurrect it
    removeLocalSession(sessionId);
  },

  /** Hide a tab locally while its server session intentionally keeps running
   * (active-goal detach). This persisted dismissal is honored by boot hydration. */
  dismissSession(sessionId: string) {
    locallyDismissedIds.add(sessionId);
    locallyDeletedIds.add(sessionId);
    persistDismissedIds(locallyDismissedIds);
    removeLocalSession(sessionId);
  },

  /** Explicit reopen lifecycle for future goal/history entry points: clear the
   * local dismissal first, then fetch/hydrate the same durable session id. */
  restoreDismissedSession(sessionId: string) {
    locallyDismissedIds.delete(sessionId);
    locallyDeletedIds.delete(sessionId);
    persistDismissedIds(locallyDismissedIds);
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

  setServerTurnControl(control: ServerTurnControlState) {
    if (!control.sessionId) return;
    setState((current) => {
      const serverTurnControls = { ...current.serverTurnControls };
      if (control.controllable && control.operatorControllable && control.taskId) {
        serverTurnControls[control.sessionId] = control;
      } else {
        delete serverTurnControls[control.sessionId];
      }
      return { ...current, serverTurnControls };
    });
  },

  clearServerTurnControl(sessionId: string, taskId?: string) {
    if (!sessionId) return;
    setState((current) => {
      const existing = current.serverTurnControls[sessionId];
      if (!existing || (taskId && existing.taskId !== taskId)) return current;
      const serverTurnControls = { ...current.serverTurnControls };
      delete serverTurnControls[sessionId];
      return { ...current, serverTurnControls };
    });
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
