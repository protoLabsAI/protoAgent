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

// Guards the one-time migration against a concurrent double-push (two resync triggers firing
// close together) WITHOUT latching persistently until the push succeeds — so a failed push still
// retries on the next boot. Cleared on both success and failure; the persistent latch is what
// stops future boots once the server actually has the list.
let migrating = false;

/** Register a session server-side on its first send (r3). The title is the tab's own when the
 *  operator already named it, else derived from the send text exactly as the first-message
 *  auto-title is — so the server and every other device title the tab identically. */
export function registerSessionOnSend(session: Pick<ChatSession, "id" | "title" | "createdAt">, text: string): void {
  if (registered.has(session.id)) return;
  registered.add(session.id);
  const title = session.title && session.title !== DEFAULT_SESSION_TITLE ? session.title : sessionTitleFromText(text);
  void api
    .registerSession(session.id, { title, createdAt: new Date(session.createdAt).toISOString() })
    .catch(() => registered.delete(session.id)); // let the next send retry
}

/** Push an operator rename so the title shows on every device (r3). Fire-and-forget — the
 *  local store already applied it, and the server copy wins on the next read. */
export function pushRename(sessionId: string, title: string): void {
  registered.add(sessionId);
  void api.updateSession(sessionId, { title: title.trim() || DEFAULT_SESSION_TITLE }).catch(() => {});
}

/** Close a tab everywhere (r1): server-close so the `session.closed` event removes it on every
 *  device. Non-destructive — the chat stays retrievable and its turns are untouched. */
export function pushClose(sessionId: string): void {
  void api.updateSession(sessionId, { closed: true }).catch(() => {});
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
    .sessions.filter((session) => session.messages.length > 0 || session.title !== DEFAULT_SESSION_TITLE);
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

/** Test seam: forget the in-memory "already registered" set and the migration in-flight guard. */
export function resetSessionListSyncForTests(): void {
  registered.clear();
  migrating = false;
}
