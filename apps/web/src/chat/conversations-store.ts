import { useMemo, useSyncExternalStore } from "react";

import { api } from "../lib/api";
import { onConnectionChange, onTopic } from "../lib/events";
import { readKey, writeKey } from "../lib/storage";
import type { ConversationRow } from "../lib/api/chat";

// The console's data layer for "Other conversations" (docs/design/other-conversations.md, slice
// C1). It owns the list of the agent's OUTSIDE A2A threads — hydrated from `GET /api/conversations`
// (chatApi.listConversations), paged older with `before=`, and kept live by the bus's a2a
// `turn.started` / `turn.finished` events — plus the Conversations tab's client-only UI state:
// whether it is hidden (per device), and the stage (`chat` vs `conversations`) with the open
// thread, kept SEPARATE from the chat store's `currentSessionId` (§4.7).
//
// Nothing is rendered here (that lands in a later slice); this is the store the tab, the list and
// the thread pane will read. Modelled on backgroundJobStore.ts: hydrate once, stay live over the
// bus, never poll, and don't let a slow refetch clobber a newer live event.

export type { ConversationRow } from "../lib/api/chat";

/** The Conversations tab is client-only and NOT a chat (§4.7). Its stage id is reserved: it is
 *  never sent to the server, never written to the S6 chat list, and never counted as a chat. The
 *  chat store keeps `currentSessionId` pointing at the last chat; this value says which is showing. */
export const CONVERSATIONS_STAGE_ID = "conversations";

export type Stage = "chat" | "conversations";

/** True for the reserved stage id — the guard every send / chat-list / "unused blank chat" path
 *  uses so the Conversations tab's id can never leak to the server (§4.7). */
export function isConversationsStage(id: string | null | undefined): boolean {
  return id === CONVERSATIONS_STAGE_ID;
}

export type ConversationsStatus = "idle" | "loading" | "ready" | "error";

export type ConversationsState = {
  status: ConversationsStatus;
  /** Every loaded row, newest-active first, `for_you` before `automated` (server order), with
   *  older pages appended. Grouped for consumers by `forYouRows` / `automatedRows`. */
  rows: ConversationRow[];
  /** The last windowed/older fetch filled a whole page, so there may be more to page back to. */
  hasOlder: boolean;
  loadingOlder: boolean;
  /** This device hid the tab (per-device localStorage, §3.2). */
  hidden: boolean;
  stage: Stage;
  /** The open conversation's context id inside the tab, or null for the list view (§3.4). Never
   *  the reserved stage id — a real A2A context id. */
  openThreadId: string | null;
  /** A deep link (§4.6) forced the tab open: it shows even when hidden or empty, and while loading. */
  deepLink: boolean;
  /** The server's degraded reason (task store down, read failed), when the list came back empty. */
  reason?: string;
};

// The 7-day window the tab's `count` is measured in (§3.1, §4.3) — the same window the server's
// default (un-paged) fetch returns. Rows paged in with `before=` fall outside it and don't count.
const WINDOW_MS = 7 * 24 * 60 * 60 * 1000;
const PAGE = 50;

// Per-agent, per-device key for the hidden flag (registered in lib/storage.ts). Built like the
// chat store's key so hiding on one member's console never hides it on another's.
const HIDDEN_KEY = (() => {
  try {
    const m = globalThis.location?.pathname.match(/\/agent\/([^/?#]+)/);
    return m ? `protoagent.conversations.hidden:${decodeURIComponent(m[1])}` : "protoagent.conversations.hidden";
  } catch {
    return "protoagent.conversations.hidden";
  }
})();

function loadHidden(): boolean {
  return readKey("local", HIDDEN_KEY) === "1";
}

let state: ConversationsState = {
  status: "idle",
  rows: [],
  hasOlder: false,
  loadingOlder: false,
  hidden: loadHidden(),
  stage: "chat",
  openThreadId: null,
  deepLink: false,
};

const listeners = new Set<() => void>();
// When each row last changed from a LIVE bus event — so a hydration that started before an event
// landed never overlays the newer `active` / `last_state` with the row it read (backgroundJobStore
// pattern). Keyed by context id.
const liveAt = new Map<string, number>();
// Context ids learned via `loadOlder` (outside the 7-day window). A windowed refetch keeps them
// around rather than dropping them, so paging back then refreshing doesn't lose older rows.
const olderIds = new Set<string>();
let started = false;
let stop: (() => void) | null = null;
let fetchSeq = 0; // guards against an out-of-order windowed refetch landing after a newer one

function emit() {
  for (const l of listeners) l();
}

function set(patch: Partial<ConversationsState>) {
  state = { ...state, ...patch };
  emit();
}

function indexOf(contextId: string): number {
  return state.rows.findIndex((r) => r.session_id === contextId);
}

/** Merge a windowed fetch into the store, preserving older-paged rows and any row a live event
 *  touched after the fetch began (so a slow response never resurrects a stale status). */
function ingestWindow(fetched: ConversationRow[], startedAt: number) {
  const prevById = new Map(state.rows.map((r) => [r.session_id, r]));
  const rows: ConversationRow[] = [];
  const seen = new Set<string>();
  for (const r of fetched) {
    const prev = prevById.get(r.session_id);
    // A live event beat this response home — keep what it set, take the rest from the server.
    const row =
      prev && (liveAt.get(r.session_id) ?? 0) >= startedAt
        ? { ...r, active: prev.active, last_state: prev.last_state }
        : r;
    rows.push(row);
    seen.add(r.session_id);
  }
  // Keep rows paged in from before the window that this windowed read doesn't mention.
  for (const r of state.rows) {
    if (!seen.has(r.session_id) && olderIds.has(r.session_id)) rows.push(r);
  }
  return rows;
}

function refetch(initial: boolean) {
  const startedAt = Date.now();
  const seq = ++fetchSeq;
  if (initial) set({ status: "loading" });
  api
    .listConversations({ limit: PAGE })
    .then((d) => {
      if (seq !== fetchSeq) return; // a newer fetch already landed
      const fetched = d.conversations || [];
      set({
        status: "ready",
        rows: ingestWindow(fetched, startedAt),
        hasOlder: fetched.length >= PAGE,
        reason: d.reason,
      });
    })
    .catch(() => {
      if (seq !== fetchSeq) return;
      // Only the FIRST load surfaces an error; a background refetch keeps the rows it has
      // (best-effort, like the session index) rather than blanking a live list on a blip.
      if (state.status !== "ready") set({ status: "error" });
    });
}

/** Re-run the initial windowed fetch after a failure — the list pane's "Try again" (§5). Shows
 *  the loading state again, then ready or error. Safe to call anytime; a newer fetch wins. */
export function retry() {
  refetch(true);
}

/** Page back 50 older rows with `before=<oldest last_updated>` (§4.3). */
function loadOlder() {
  if (state.loadingOlder || !state.rows.length) return;
  const oldest = [...state.rows].map((r) => r.last_updated).filter((t): t is string => !!t).sort()[0];
  if (!oldest) return;
  set({ loadingOlder: true });
  api
    .listConversations({ before: oldest, limit: PAGE })
    .then((d) => {
      const fetched = d.conversations || [];
      const known = new Set(state.rows.map((r) => r.session_id));
      const fresh = fetched.filter((r) => !known.has(r.session_id));
      for (const r of fresh) olderIds.add(r.session_id);
      set({ rows: [...state.rows, ...fresh], hasOlder: fetched.length >= PAGE, loadingOlder: false });
    })
    .catch(() => set({ loadingOlder: false }));
}

// ── live updates ────────────────────────────────────────────────────────────────────────────
// a2a `turn.started` / `turn.finished` (src/server/a2a.py) update a known row's `active` /
// `last_state`; an unknown context STARTING triggers a windowed refetch — that is how a thread's
// tab first appears. `turn.input_required` flips a known row to Needs you (no `turn.finished` is
// published for a park, so without it a hidden tab could never return on its own, §3.2).
// `turn.resumed` is the park's counterpart: when the operator answers, the server publishes it
// (and suppresses `turn.started` for that leg), so the known row goes back to Running and clears
// Needs you WITHOUT waiting for the resumed turn's terminal frame. No polling.

function onStarted(d: Record<string, unknown>) {
  if (d.origin !== "a2a") return;
  const id = String(d.session_id || "");
  if (!id) return;
  const i = indexOf(id);
  if (i < 0) {
    refetch(false); // unknown context — refetch so its row (and the tab) appears
    return;
  }
  liveAt.set(id, Date.now());
  const rows = state.rows.slice();
  rows[i] = { ...rows[i], active: true };
  set({ rows });
}

function onFinished(d: Record<string, unknown>) {
  if (d.origin !== "a2a") return;
  const id = String(d.session_id || "");
  const i = id ? indexOf(id) : -1;
  if (i < 0) return; // a finish for a thread we don't list — nothing to update
  liveAt.set(id, Date.now());
  const rows = state.rows.slice();
  rows[i] = { ...rows[i], active: false, last_state: String(d.state || "") || rows[i].last_state };
  set({ rows });
}

function onInputRequired(d: Record<string, unknown>) {
  // This event carries `context_id` (every origin), not `session_id`, and no `turn.finished`
  // follows a park — so flip the KNOWN row to Needs you here. Unknown contexts (e.g. a console
  // chat, never listed here) are ignored; a listed thread that parks was started earlier.
  const id = String(d.context_id || "");
  const i = id ? indexOf(id) : -1;
  if (i < 0) return;
  liveAt.set(id, Date.now());
  const rows = state.rows.slice();
  rows[i] = { ...rows[i], last_state: "input-required" };
  set({ rows });
}

function onResumed(d: Record<string, unknown>) {
  // The operator answered a parked thread: the server publishes `turn.resumed` (carrying
  // `context_id`, like `turn.input_required`) and does NOT publish a fresh `turn.started` for the
  // resumed leg. So flip the KNOWN row back to Running and off Needs you here — otherwise the row
  // keeps `last_state: "input-required"` (and `active` whatever the first leg left) until the
  // resumed turn's terminal `turn.finished` lands, stranding a hidden tab open on answered work.
  // The turn is running again, so `last_state` is `working`. Unknown contexts are ignored (a
  // resume implies an earlier park/start already put the row here).
  const id = String(d.context_id || "");
  const i = id ? indexOf(id) : -1;
  if (i < 0) return;
  liveAt.set(id, Date.now());
  const rows = state.rows.slice();
  rows[i] = { ...rows[i], active: true, last_state: "working" };
  set({ rows });
}

function start() {
  if (started) return;
  started = true;
  refetch(true);
  // Refetch on reconnect (a fresh stream replays nothing without a cursor, ADR 0130 D3) and on a
  // server-signalled resync. `onConnectionChange` reports the current state at subscribe time,
  // which the `initial` flag skips so startup stays one request.
  let initial = true;
  const offConn = onConnectionChange((c) => {
    if (initial) {
      initial = false;
      return;
    }
    if (c) refetch(false);
  });
  const offResync = onTopic("resync_required", () => refetch(false));
  const offStart = onTopic("turn.started", onStarted);
  const offFinish = onTopic("turn.finished", onFinished);
  const offPark = onTopic("turn.input_required", onInputRequired);
  const offResume = onTopic("turn.resumed", onResumed);
  stop = () => {
    offConn();
    offResync();
    offStart();
    offFinish();
    offPark();
    offResume();
  };
}

function subscribe(l: () => void) {
  start();
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

const snapshot = () => state;

// ── derived selectors (pure; computed on read so the 7-day window tracks real time) ───────────

const PARKED = /input[-_]?required|auth[-_]?required/i;

/** Is this row within the 7-day window the tab's `count` is measured in? */
function inWindow(row: ConversationRow, now: number): boolean {
  const t = row.last_updated ? Date.parse(row.last_updated) : NaN;
  // No timestamp can't be placed in time — the windowed fetch never returns such a row, and a
  // paged-older one is outside the window by construction, so treat it as out.
  return Number.isFinite(t) && t >= now - WINDOW_MS;
}

export function forYouRows(s: ConversationsState): ConversationRow[] {
  return s.rows.filter((r) => r.group === "for_you");
}

export function automatedRows(s: ConversationsState): ConversationRow[] {
  return s.rows.filter((r) => r.group === "automated");
}

/** Rows inside the 7-day window, both groups (§3.1 / §4.3 — what makes the tab appear). */
export function conversationCount(s: ConversationsState, now: number = Date.now()): number {
  return s.rows.reduce((n, r) => (inWindow(r, now) ? n + 1 : n), 0);
}

/** Loaded rows with a turn running right now. */
export function runningCount(s: ConversationsState): number {
  return s.rows.reduce((n, r) => (r.active ? n + 1 : n), 0);
}

/** Loaded rows parked on the operator (input-required / auth-required) — "Needs you". */
export function needsYouCount(s: ConversationsState): number {
  return s.rows.reduce((n, r) => (r.last_state && PARKED.test(r.last_state) ? n + 1 : n), 0);
}

/** Whether the Conversations tab is shown (§3.1): the list has LOADED and holds at least one row
 *  in the window and the device hasn't hidden it — OR a deep link is open. A hidden tab still
 *  returns when any row needs you. While loading (and with zero rows), it is not shown. */
export function isTabVisible(s: ConversationsState, now: number = Date.now()): boolean {
  if (s.deepLink) return true;
  if (s.status !== "ready") return false;
  if (conversationCount(s, now) <= 0) return false;
  return !s.hidden || needsYouCount(s) > 0;
}

// ── mutators: hidden state + stage ────────────────────────────────────────────────────────────

/** Hide the tab on this device (§3.2) — persisted to localStorage, not the server chat list. */
export function hide() {
  writeKey("local", HIDDEN_KEY, "1");
  if (!state.hidden) set({ hidden: true });
}

/** Un-hide the tab on this device ("Show Conversations tab", §3.2). */
export function show() {
  writeKey("local", HIDDEN_KEY, "0");
  if (state.hidden) set({ hidden: false });
}

/** Switch the stage without touching the chat store's `currentSessionId` (§4.7). */
export function setStage(stage: Stage) {
  if (state.stage !== stage) set({ stage });
}

/** Open the Conversations tab's list view (stage `conversations`, no open thread). */
export function openConversations() {
  if (state.stage !== "conversations" || state.openThreadId !== null) set({ stage: "conversations", openThreadId: null });
}

/** Open a specific thread inside the tab (§3.4). `contextId` is a real A2A context id, never the
 *  reserved stage id; a guard keeps the reserved id from ever being treated as a thread. */
export function openThread(contextId: string) {
  if (isConversationsStage(contextId)) return;
  set({ stage: "conversations", openThreadId: contextId });
}

/** Back to the list from an open thread (§3.4), staying on the Conversations stage. */
export function backToList() {
  if (state.openThreadId !== null) set({ openThreadId: null });
}

/** Leave the Conversations tab for a chat (selecting any chat tab, §3.3): stage back to `chat`.
 *  The open thread is remembered so returning to the tab reopens it. */
export function openChat() {
  if (state.stage !== "chat") set({ stage: "chat" });
}

/** A deep link in (§4.6): show the tab (even if hidden/empty), select it, open the thread. The
 *  `deepLink` flag forces the tab visible on its own, so the device's persisted hide preference is
 *  left untouched — dismissing the link returns the tab to whatever hidden state it had. */
export function openDeepLink(contextId: string) {
  set({
    deepLink: true,
    stage: "conversations",
    openThreadId: isConversationsStage(contextId) ? null : contextId,
  });
}

/** Clear the deep-link override (navigating away from a linked thread, §4.6) — the tab falls back
 *  to its normal visibility rules (hidden preference + row count). */
export function clearDeepLink() {
  if (state.deepLink) set({ deepLink: false });
}

// ── hooks + test seams ──────────────────────────────────────────────────────────────────────

/** The whole store plus the derived values the tab reads. */
export function useConversations() {
  const s = useSyncExternalStore(subscribe, snapshot, snapshot);
  return useMemo(
    () => ({
      ...s,
      forYou: forYouRows(s),
      automated: automatedRows(s),
      count: conversationCount(s),
      running: runningCount(s),
      needsYou: needsYouCount(s),
      tabVisible: isTabVisible(s),
      loadOlder,
    }),
    [s],
  );
}

/** The live row for one thread by context id, or null when it isn't listed (yet). Work's
 *  provenance line (§4.6) reads its `origin_label` to name who started a waiting task's
 *  conversation, without pulling the whole list's derived shape. Shares the one hydration — a
 *  consumer here subscribes the same singleton the Conversations tab does. */
export function useConversationRow(contextId: string): ConversationRow | null {
  const s = useSyncExternalStore(subscribe, snapshot, snapshot);
  return useMemo(() => s.rows.find((r) => r.session_id === contextId) ?? null, [s.rows, contextId]);
}

/** Is this session one a PEER started — a row listed under From others? Read live (not through
 *  a hook) for the code that must decide at the moment it acts: a steer into such a thread is
 *  always sent private (#5400), and the closure sending it may be older than the newest rows.
 *  Only as good as what is loaded: a thread the list has not hydrated, or one paged out of it,
 *  reads false — the server still takes the private path for it on its own. */
export function isFromOthers(sessionId: string | null | undefined): boolean {
  return Boolean(sessionId) && indexOf(sessionId as string) !== -1;
}

export function subscribeForTest(l: () => void) {
  return subscribe(l);
}

export function snapshotForTest(): ConversationsState {
  return state;
}

export { loadOlder };

/** Reset the store between test cases. */
export function __resetForTest() {
  stop?.();
  stop = null;
  started = false;
  fetchSeq = 0;
  liveAt.clear();
  olderIds.clear();
  state = {
    status: "idle",
    rows: [],
    hasOlder: false,
    loadingOlder: false,
    hidden: loadHidden(),
    stage: "chat",
    openThreadId: null,
    deepLink: false,
  };
  emit();
}
