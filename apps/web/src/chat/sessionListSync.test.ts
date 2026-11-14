// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { readKey, removeKey } from "../lib/storage";
import type { ChatMessage } from "../lib/types";
import { chatStore, DEFAULT_SESSION_TITLE, SESSION_LIST_MIGRATED_KEY } from "./chat-store";
import {
  markServerKnownSessions,
  migrateLocalSessionsOnce,
  pendingSessionListIntents,
  pushClose,
  pushRename,
  pushReopen,
  reconcilePendingListIntents,
  registerSessionOnSend,
  resetSessionListSyncForTests,
} from "./sessionListSync";

function userMsg(content: string): ChatMessage {
  return { id: `u-${content}`, role: "user", content, status: "done" };
}

beforeEach(() => {
  resetSessionListSyncForTests();
  removeKey("local", SESSION_LIST_MIGRATED_KEY);
  vi.spyOn(api, "registerSession").mockResolvedValue({ ok: true, session_id: "" });
  vi.spyOn(api, "updateSession").mockResolvedValue({ ok: true, session_id: "" });
  vi.spyOn(api, "migrateSessions").mockResolvedValue({ ok: true, migrated: 0 });
});

afterEach(() => vi.restoreAllMocks());

// ── syncServerSessionList: the store follows the server list (r3) ───────────────

describe("syncServerSessionList", () => {
  it("adds a registered turnless session this device lacks, with its server title", () => {
    const id = "chat-remote-add";
    chatStore.syncServerSessionList({
      summaries: [{ session_id: id, title: "From the phone", turn_count: 0, created_at: "2026-11-13T00:00:00Z" }],
      closedIds: [],
    });
    const added = chatStore.getSnapshot().sessions.find((s) => s.id === id);
    expect(added).toMatchObject({ id, title: "From the phone", messages: [] });
  });

  it("does not add a turn-bearing session (durable hydration owns it)", () => {
    const id = "chat-has-turns";
    chatStore.syncServerSessionList({
      summaries: [{ session_id: id, title: "Has turns", turn_count: 3 }],
      closedIds: [],
    });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(false);
  });

  it("applies a server rename but never clobbers a real local title with the default placeholder", () => {
    const id = "chat-title";
    chatStore.syncServerSessionList({ summaries: [{ session_id: id, title: "Original", turn_count: 0 }], closedIds: [] });
    // Server renamed it → the tab follows.
    chatStore.syncServerSessionList({ summaries: [{ session_id: id, title: "Renamed on desktop", turn_count: 0 }], closedIds: [] });
    expect(chatStore.getSnapshot().sessions.find((s) => s.id === id)?.title).toBe("Renamed on desktop");
    // A stale read carrying the default placeholder must not wipe the real title.
    chatStore.syncServerSessionList({ summaries: [{ session_id: id, title: DEFAULT_SESSION_TITLE, turn_count: 0 }], closedIds: [] });
    expect(chatStore.getSnapshot().sessions.find((s) => s.id === id)?.title).toBe("Renamed on desktop");
  });

  it("removes a tab the server reports closed", () => {
    const id = "chat-to-close";
    chatStore.syncServerSessionList({ summaries: [{ session_id: id, title: "Doomed tab", turn_count: 0 }], closedIds: [] });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(true);
    chatStore.syncServerSessionList({ summaries: [], closedIds: [id] });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(false);
    // And a later index that still names it closed keeps it gone (not re-added).
    chatStore.syncServerSessionList({ summaries: [{ session_id: id, title: "Doomed tab", turn_count: 0 }], closedIds: [id] });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(false);
  });

  it("leaves a session the server does not name alone (stays local until its first send)", () => {
    const local = chatStore.createSession();
    chatStore.renameSession(local.id, "Local only");
    chatStore.syncServerSessionList({ summaries: [], closedIds: [] });
    expect(chatStore.getSnapshot().sessions.find((s) => s.id === local.id)?.title).toBe("Local only");
  });

  it("re-shows a server-open session this device had dismissed, clearing the dismissal (server wins)", () => {
    const id = "chat-reopened-elsewhere";
    chatStore.syncServerSessionList({ summaries: [{ session_id: id, title: "Shared", turn_count: 0 }], closedIds: [] });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(true);
    // Closed here (a local dismissal mirroring a server close) — the tab drops and is remembered.
    chatStore.syncServerSessionList({ summaries: [], closedIds: [id] });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(false);
    // Reopened on ANOTHER device → the server reports it OPEN again. This device missed the
    // session.reopened event, but a full resync (boot/focus) must re-show it: the server copy wins,
    // so the persisted dismissal is cleared rather than keeping the chat hidden here forever.
    chatStore.syncServerSessionList({ summaries: [{ session_id: id, title: "Shared", turn_count: 0 }], closedIds: [] });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(true);
  });

  it("does NOT clear a dismissal when the server's closed list is not authoritative (closedKnown false, r5)", () => {
    const id = "chat-closed-but-leaked";
    // Shown, then closed here (dismissed + remembered as a local tombstone).
    chatStore.syncServerSessionList({ summaries: [{ session_id: id, title: "Shared", turn_count: 0 }], closedIds: [] });
    chatStore.syncServerSessionList({ summaries: [], closedIds: [id] });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(false);
    // The index could NOT read its server-held list (registry failure): the closed chat LEAKS back
    // into `summaries` as an apparent open session, and the server's `closed` array is unknown
    // (closedKnown false). Clearing the dismissal here would un-hide a tab the user closed, so the
    // server-wins un-dismiss MUST be skipped — the tab stays hidden and the dismissal is intact.
    chatStore.syncServerSessionList({
      summaries: [{ session_id: id, title: "Shared", turn_count: 0 }],
      closedIds: [],
      closedKnown: false,
    });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(false);
    // Once a TRUSTWORTHY read reports it genuinely open again, the server wins and the tab returns.
    chatStore.syncServerSessionList({
      summaries: [{ session_id: id, title: "Shared", turn_count: 0 }],
      closedIds: [],
      closedKnown: true,
    });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(true);
  });

  // ── the swallow-then-revert race (review round 2): a close/rename this device pushed must not
  //    be reverted by a resync that races it, and it must still converge once the server confirms.
  it("keeps a tab this device closed hidden on resync until the server confirms the close", () => {
    const id = "chat-closed-here";
    chatStore.syncServerSessionList({ summaries: [{ session_id: id, title: "Shared", turn_count: 0 }], closedIds: [] });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(true);
    // The user closes it here: the local dismissal, plus a server-close push that is still in
    // flight (or failed) — the index has NOT caught up.
    chatStore.dismissSession(id);
    pushClose(id);
    // A resync races the close: the index STILL reports the session OPEN. Without the pending guard
    // this cleared the dismissal and re-showed the tab the user just closed — the revert.
    const racyOpen = { summaries: [{ session_id: id, title: "Shared", turn_count: 0 }], closedIds: [] };
    reconcilePendingListIntents(racyOpen);
    chatStore.syncServerSessionList({ ...racyOpen, pending: pendingSessionListIntents() });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(false);
    // The server catches up (reports it closed). The pending intent clears → server-wins resumes.
    reconcilePendingListIntents({ summaries: [], closedIds: [id] });
    chatStore.syncServerSessionList({ summaries: [], closedIds: [id], pending: pendingSessionListIntents() });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(false);
    expect(pendingSessionListIntents().close.has(id)).toBe(false);
    // And now a GENUINE reopen elsewhere (server reports OPEN) re-shows it — the guard is gone.
    reconcilePendingListIntents(racyOpen);
    chatStore.syncServerSessionList({ ...racyOpen, pending: pendingSessionListIntents() });
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === id)).toBe(true);
  });

  it("keeps a rename this device pushed until the server reflects it (no revert to the stale title)", () => {
    const id = "chat-renamed-here";
    chatStore.syncServerSessionList({ summaries: [{ session_id: id, title: "Old", turn_count: 0 }], closedIds: [] });
    markServerKnownSessions([id]); // the server named it, so renaming it propagates (not local-only)
    chatStore.renameSession(id, "New");
    pushRename(id, "New");
    // A resync races it: the index still carries the OLD title. The guard keeps the local "New".
    reconcilePendingListIntents({ summaries: [{ session_id: id, title: "Old" }], closedIds: [] });
    chatStore.syncServerSessionList({
      summaries: [{ session_id: id, title: "Old", turn_count: 0 }],
      closedIds: [],
      pending: pendingSessionListIntents(),
    });
    expect(chatStore.getSnapshot().sessions.find((s) => s.id === id)?.title).toBe("New");
    // The server catches up to "New": the pending title clears and server-wins resumes.
    reconcilePendingListIntents({ summaries: [{ session_id: id, title: "New" }], closedIds: [] });
    chatStore.syncServerSessionList({
      summaries: [{ session_id: id, title: "New", turn_count: 0 }],
      closedIds: [],
      pending: pendingSessionListIntents(),
    });
    expect(chatStore.getSnapshot().sessions.find((s) => s.id === id)?.title).toBe("New");
    expect(pendingSessionListIntents().title.has(id)).toBe(false);
  });
});

// ── the write side: register on send, rename/close push, migrate once ───────────

describe("session-list push", () => {
  it("registers a session on its first send, titled from the send text, only once", () => {
    const session = { id: "chat-first-send", title: DEFAULT_SESSION_TITLE, createdAt: Date.UTC(2026, 10, 13) };
    registerSessionOnSend(session, "What's new in the fleet?");
    registerSessionOnSend(session, "a second send"); // already registered → no second call
    expect(api.registerSession).toHaveBeenCalledTimes(1);
    expect(api.registerSession).toHaveBeenCalledWith("chat-first-send", {
      title: "What's new in the fleet?",
      createdAt: new Date(session.createdAt).toISOString(),
    });
  });

  it("prefers the tab's own name over the send text when the operator already named it", () => {
    registerSessionOnSend({ id: "chat-named", title: "Sprint planning", createdAt: 0 }, "some message");
    expect(api.registerSession).toHaveBeenCalledWith("chat-named", expect.objectContaining({ title: "Sprint planning" }));
  });

  it("never registers an INCOGNITO session (its title must not leak to other devices)", () => {
    registerSessionOnSend({ id: "chat-incog", title: DEFAULT_SESSION_TITLE, createdAt: 0, incognito: true }, "private");
    expect(api.registerSession).not.toHaveBeenCalled();
  });

  it("pushes a rename for a SENT (server-known) tab, and a close", () => {
    // A sent tab has turns → the server already knows it, so the rename propagates to every device.
    const sent = chatStore.createSession();
    chatStore.updateMessages(sent.id, [userMsg("Deploy status")]);
    pushRename(sent.id, "Renamed");
    expect(api.updateSession).toHaveBeenCalledWith(sent.id, { title: "Renamed" });
    pushClose("chat-c");
    expect(api.updateSession).toHaveBeenCalledWith("chat-c", { closed: true });
  });

  it("does NOT push a rename for a never-sent local tab (stays local-only until its first send, r3)", () => {
    // rename_session registers an unknown session as an OPEN row, so pushing a never-sent tab's
    // rename would surface an empty, never-sent tab on every device — the leak the gate prevents.
    const localOnly = chatStore.createSession();
    pushRename(localOnly.id, "Just a draft");
    expect(api.updateSession).not.toHaveBeenCalled();
    // Once the server itself names it (via the index / change feed), a later rename DOES propagate.
    markServerKnownSessions([localOnly.id]);
    pushRename(localOnly.id, "Now shared");
    expect(api.updateSession).toHaveBeenCalledWith(localOnly.id, { title: "Now shared" });
  });

  it("never pushes a rename for an INCOGNITO tab (renamed locally only, no server row)", () => {
    const incog = chatStore.createSession({ incognito: true });
    pushRename(incog.id, "Secret project");
    expect(api.updateSession).not.toHaveBeenCalled();
  });

  it("pushReopen marks the session open and supersedes a pending close", () => {
    pushClose("chat-x");
    expect(pendingSessionListIntents().close.has("chat-x")).toBe(true);
    pushReopen("chat-x");
    expect(pendingSessionListIntents().close.has("chat-x")).toBe(false);
    expect(pendingSessionListIntents().open.has("chat-x")).toBe(true);
    expect(api.updateSession).toHaveBeenLastCalledWith("chat-x", { closed: false });
  });

  it("re-pushes a still-diverged close on reconcile, and clears it once the server confirms", () => {
    pushClose("chat-stuck");
    const update = api.updateSession as unknown as ReturnType<typeof vi.fn>;
    update.mockClear();
    // The index still reports it OPEN → the dropped close is re-pushed (retry rides the resync).
    reconcilePendingListIntents({ summaries: [{ session_id: "chat-stuck", title: "x" }], closedIds: [] });
    expect(update).toHaveBeenCalledWith("chat-stuck", { closed: true });
    // Once the server reports it closed, the intent clears and nothing is re-pushed.
    update.mockClear();
    reconcilePendingListIntents({ summaries: [], closedIds: ["chat-stuck"] });
    expect(update).not.toHaveBeenCalled();
    expect(pendingSessionListIntents().close.has("chat-stuck")).toBe(false);
  });

  it("migrates this device's non-empty open tabs exactly once, skipping empty placeholders", async () => {
    const chat = chatStore.createSession();
    chatStore.updateMessages(chat.id, [userMsg("Deploy status")]);
    const placeholder = chatStore.createSession(); // empty, never named → not a chat to keep
    await migrateLocalSessionsOnce();
    expect(api.migrateSessions).toHaveBeenCalledTimes(1);
    const sent = (api.migrateSessions as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as {
      session_id: string;
      closed?: boolean;
    }[];
    expect(sent.some((s) => s.session_id === chat.id)).toBe(true);
    // The never-named empty placeholder is NOT in the payload (the rule this test actually proves).
    expect(sent.some((s) => s.session_id === placeholder.id)).toBe(false);
    // Latched: a second boot does not push again.
    await migrateLocalSessionsOnce();
    expect(api.migrateSessions).toHaveBeenCalledTimes(1);
  });

  it("excludes an INCOGNITO tab from the one-time migration (no title leak)", async () => {
    const normal = chatStore.createSession();
    chatStore.updateMessages(normal.id, [userMsg("Normal chat")]);
    const incog = chatStore.createSession({ incognito: true });
    chatStore.updateMessages(incog.id, [userMsg("Private thoughts")]);
    await migrateLocalSessionsOnce();
    const sent = (api.migrateSessions as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as {
      session_id: string;
    }[];
    expect(sent.some((s) => s.session_id === normal.id)).toBe(true);
    expect(sent.some((s) => s.session_id === incog.id)).toBe(false);
  });

  it("migrates a locally dismissed tab as CLOSED so the dismissal is preserved everywhere (r4)", async () => {
    const keep = chatStore.createSession();
    chatStore.updateMessages(keep.id, [userMsg("Keep this one open")]);
    const detached = chatStore.createSession();
    chatStore.renameSession(detached.id, "Detached goal");
    chatStore.dismissSession(detached.id); // device-local dismissal, never pushed before S6
    await migrateLocalSessionsOnce();
    const sent = (api.migrateSessions as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as {
      session_id: string;
      closed?: boolean;
    }[];
    // The open tab goes up OPEN; the dismissal goes up CLOSED — not re-opened on other devices.
    expect(sent.find((s) => s.session_id === keep.id)?.closed).toBeFalsy();
    expect(sent.find((s) => s.session_id === detached.id)).toMatchObject({ closed: true });
  });

  it("does not latch when the migrate push fails, so a later boot retries and then latches (r5)", async () => {
    const chat = chatStore.createSession();
    chatStore.updateMessages(chat.id, [userMsg("Must reach the server")]);
    (api.migrateSessions as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("offline at boot"));
    await migrateLocalSessionsOnce();
    expect(api.migrateSessions).toHaveBeenCalledTimes(1);
    expect(readKey("local", SESSION_LIST_MIGRATED_KEY)).toBeFalsy(); // NOT latched — a failed push drops nothing
    // A later boot retries and, on success, latches.
    await migrateLocalSessionsOnce();
    expect(api.migrateSessions).toHaveBeenCalledTimes(2);
    expect(readKey("local", SESSION_LIST_MIGRATED_KEY)).toBe("1");
    // Now latched — a third boot does not push again.
    await migrateLocalSessionsOnce();
    expect(api.migrateSessions).toHaveBeenCalledTimes(2);
  });
});
