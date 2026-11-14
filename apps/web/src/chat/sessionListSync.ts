// The WRITE half of server-authoritative session-list sync (ADR 0130 S6). The console pushes
// its list intents to the server so every device converges: a session registers on its FIRST
// send, a rename and a close propagate, and a one-time migration seeds the server-held list with
// this device's open tabs + titles before it becomes authoritative. The READ half — following
// the server's list on events / boot / focus / resync — lives in sessionHydration.ts; this module
// stays import-light (api + the store) so it can be called from the send path and the tab bar.

import { api } from "../lib/api";
import {
  chatStore,
  DEFAULT_SESSION_TITLE,
  dismissedSessionIds,
  SESSION_LIST_MIGRATED_KEY,
  sessionTitleFromText,
  type ChatSession,
} from "./chat-store";
import { readKey, writeKey } from "../lib/storage";

// Sessions this device has already registered server-side (ADR 0130 S6, r3: "stays local only
// until its first send, then registers"). In-memory — a failed register drops back out so the
// next send retries; the server dedupes a repeat regardless.
const registered = new Set<string>();

// List intents this device applied LOCALLY and pushed, but that a server read has not reflected
// back yet. Between the push and a confirming read, the READ side (syncServerSessionList) DEFERS
// to these so a stale index read — one issued before the push landed, or a push still in flight or
// transiently failed — can't revert the intent: it won't clear the dismissal of a tab this device
// just closed, treat a tab this device just reopened as closed, or clobber a just-applied rename
// back to the stale server title. An id stays here until a server read CONFIRMS the intent
// (reconcilePendingListIntents), at which point the server copy wins again; while it diverges,
// every resync re-pushes it, so a close/rename/reopen lost to a transient failure is retried
// rather than silently dropped. This is the fix for the swallow-then-revert race (review round 2).
const pendingClose = new Set<string>();
const pendingOpen = new Set<string>();
const pendingTitle = new Map<string, string>();

// Guards the one-time migration against a concurrent double-push (two resync triggers firing
// close together) WITHOUT latching persistently until the push succeeds — so a failed push still
// retries on the next boot. Cleared on both success and failure; the persistent latch is what
// stops future boots once the server actually has the list.
let migrating = false;

/** Register a session server-side on its first send (r3). The title is the tab's own when the
 *  operator already named it, else derived from the send text exactly as the first-message
 *  auto-title is — so the server and every other device title the tab identically. An INCOGNITO
 *  session is never registered: its title (derived from the operator's first message) must not be
 *  persisted server-side or published to other devices — incognito threads leave no trace
 *  (ADR 0069 D3b). */
export function registerSessionOnSend(
  session: Pick<ChatSession, "id" | "title" | "createdAt" | "incognito">,
  text: string,
): void {
  if (session.incognito || registered.has(session.id)) return;
  registered.add(session.id);
  const title = session.title && session.title !== DEFAULT_SESSION_TITLE ? session.title : sessionTitleFromText(text);
  void api
    .registerSession(session.id, { title, createdAt: new Date(session.createdAt).toISOString() })
    .catch(() => registered.delete(session.id)); // let the next send retry
}

/** Send one list PATCH, swallowing the error — the pending set keeps the local intent winning
 *  until a read confirms it, and a diverged intent is re-pushed on the next resync, so a dropped
 *  request self-heals rather than stranding the local and server copies out of step. */
function pushListPatch(sessionId: string, patch: { title?: string; closed?: boolean }): void {
  void api.updateSession(sessionId, patch).catch(() => {});
}

/** Mark sessions the SERVER already holds (named in the index / change feed) as server-known, so a
 *  later rename of one — e.g. a turnless tab another device created and this device adopted — still
 *  propagates, while a never-sent LOCAL tab does not (the pushRename gate below). Called by
 *  hydrateDurableChatSessions with the index summaries. */
export function markServerKnownSessions(ids: Iterable<string>): void {
  for (const id of ids) if (id) registered.add(id);
}

/** Push an operator rename so the title shows on every device (r3). Held PENDING until a server
 *  read reports the new title: a resync reading the stale (pre-rename) title must not clobber the
 *  local one back — the same swallow-then-revert race a close has (review round 2) — and a failed
 *  push is retried on the next resync. */
export function pushRename(sessionId: string, title: string): void {
  const session = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
  // An INCOGNITO tab is renamed LOCALLY only: pushing it would CREATE a server-held row carrying
  // the operator's title (rename_session registers an unknown session), leaking an incognito chat
  // to every device — the same leak the send-time guard prevents (ADR 0069 D3b).
  if (session?.incognito) return;
  // A session the server doesn't know stays LOCAL-ONLY until its first send (r3). Renaming a
  // never-sent empty tab must NOT PATCH the server: rename_session registers an unknown session as
  // an OPEN row, so the rename alone would surface an empty, never-sent tab on every device. Push
  // only once the tab is server-known — it has turns (was sent), or was registered/migrated, or the
  // server's own index/events named it (markServerKnownSessions). A rename made before the first
  // send is carried into registerSessionOnSend's title instead, so it is not lost.
  if (!registered.has(sessionId) && !(session?.messages.length ?? 0)) return;
  registered.add(sessionId);
  const wanted = title.trim() || DEFAULT_SESSION_TITLE;
  pendingTitle.set(sessionId, wanted);
  pushListPatch(sessionId, { title: wanted });
}

/** Close a tab everywhere (r1): server-close so the `session.closed` event removes it on every
 *  device. Non-destructive — the chat stays retrievable and its turns are untouched. Held PENDING
 *  until a server read reports it closed, so a stale index read (issued before the close landed, or
 *  a close whose PATCH is still retrying) can't re-show the tab the user closed; the retry rides the
 *  resync (reconcilePendingListIntents). */
export function pushClose(sessionId: string): void {
  pendingOpen.delete(sessionId);
  pendingClose.add(sessionId);
  pushListPatch(sessionId, { closed: true });
}

/** Reopen a tab everywhere: the server marks it open (mirroring a `session.reopened`) so other
 *  devices re-show it. Used when the console restores a server-closed chat LOCALLY (openSourceChat)
 *  — without the server reopen, the next resync reading the still-closed server copy would drop the
 *  tab again (cross-file review). Held PENDING until a read reports it open. */
export function pushReopen(sessionId: string): void {
  pendingClose.delete(sessionId);
  pendingOpen.add(sessionId);
  pushListPatch(sessionId, { closed: false });
}

/** The still-unconfirmed list intents, for syncServerSessionList to defer to. Returned as the live
 *  references (read-only to the caller) so the guard always sees the current pending state. */
export function pendingSessionListIntents(): {
  close: ReadonlySet<string>;
  open: ReadonlySet<string>;
  title: ReadonlyMap<string, string>;
} {
  return { close: pendingClose, open: pendingOpen, title: pendingTitle };
}

/** Against the index just read, clear every pending intent the server now reflects (the server
 *  copy wins again from here) and RE-PUSH any that still diverge, so a close/rename/reopen lost to a
 *  transient offline/5xx reaches the server on a later resync instead of being silently dropped.
 *  Called by hydrateDurableChatSessions before it hands the pending set to syncServerSessionList. */
export function reconcilePendingListIntents({
  summaries,
  closedIds,
  closedKnown = true,
}: {
  summaries: { session_id: string; title?: string | null }[];
  closedIds: string[];
  // Whether the server's closed list is authoritative this read (see syncServerSessionList). When
  // false the index couldn't read its list, so a CLOSED session may masquerade as open in
  // `summaries`: reconciling against it could falsely clear a pending open/close/title intent as
  // "confirmed". Leave every pending intent in place — the next trustworthy read reconciles it, and
  // meanwhile the intent keeps winning locally rather than being silently dropped (ADR 0130 S6, r5).
  closedKnown?: boolean;
}): void {
  if (!closedKnown) return;
  const closed = new Set(closedIds.filter((id): id is string => typeof id === "string" && !!id));
  const openTitle = new Map(summaries.map((s) => [s.session_id, (s.title ?? "").trim()]));
  for (const id of [...pendingClose]) {
    if (closed.has(id)) pendingClose.delete(id); // server reflects the close now → server-wins resumes
    else pushListPatch(id, { closed: true }); // still open server-side → retry the close
  }
  for (const id of [...pendingOpen]) {
    if (openTitle.has(id) && !closed.has(id)) pendingOpen.delete(id);
    else pushListPatch(id, { closed: false });
  }
  for (const [id, wanted] of [...pendingTitle]) {
    if (openTitle.get(id) === wanted) pendingTitle.delete(id);
    else pushListPatch(id, { title: wanted });
  }
}

/** The one-time migration (r4): push this device's open tabs + titles AND its local dismissals so
 *  nothing only it knew is lost when the list becomes server-authoritative — an open tab becomes a
 *  server-held OPEN row, a dismissal a CLOSED one, so the dismissal isn't silently re-opened on
 *  other devices. Empty, never-named placeholder tabs are not chats to keep, so they are skipped.
 *  Latched so it runs exactly once per device, but ONLY once the push succeeds: latching before the
 *  network call would let one offline/5xx boot permanently drop this device's titles and tabs from
 *  the server list. After a success the server copy wins. */
export async function migrateLocalSessionsOnce(): Promise<void> {
  if (migrating || readKey("local", SESSION_LIST_MIGRATED_KEY)) return;
  const open = chatStore
    .getSnapshot()
    .sessions.filter(
      (session) =>
        // INCOGNITO tabs are never migrated: their titles would otherwise be persisted server-side
        // and published to every device, which incognito forbids (ADR 0069 D3b).
        !session.incognito && (session.messages.length > 0 || session.title !== DEFAULT_SESSION_TITLE),
    );
  // Dismissed ids are disjoint from open tabs (a dismissal removed the tab), and are migrated
  // CLOSED so the close intent is preserved everywhere, not re-opened by the server-wins read.
  const dismissed = dismissedSessionIds();
  const items: { session_id: string; title?: string; created_at?: string; closed?: boolean }[] = [
    ...open.map((session) => ({
      session_id: session.id,
      ...(session.title && session.title !== DEFAULT_SESSION_TITLE ? { title: session.title } : {}),
      created_at: new Date(session.createdAt).toISOString(),
    })),
    ...dismissed.map((id) => ({ session_id: id, closed: true })),
  ];
  if (!items.length) {
    // Nothing to migrate — latch so the (idempotent) check is skipped next boot.
    writeKey("local", SESSION_LIST_MIGRATED_KEY, "1");
    return;
  }
  migrating = true;
  try {
    await api.migrateSessions(items);
  } catch {
    // Leave the latch UNSET so the next boot retries; a failed push must not drop these tabs.
    return;
  } finally {
    migrating = false;
  }
  // Latch only after the server has the list: the migration has truly run exactly once.
  writeKey("local", SESSION_LIST_MIGRATED_KEY, "1");
  for (const session of open) registered.add(session.id);
}

/** Test seam: forget the in-memory "already registered" set, the pending list intents, and the
 *  migration in-flight guard. */
export function resetSessionListSyncForTests(): void {
  registered.clear();
  pendingClose.clear();
  pendingOpen.clear();
  pendingTitle.clear();
  migrating = false;
}
