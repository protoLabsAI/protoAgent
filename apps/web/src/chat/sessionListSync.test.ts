// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { readKey, removeKey } from "../lib/storage";
import type { ChatMessage } from "../lib/types";
import { chatStore, DEFAULT_SESSION_TITLE, SESSION_LIST_MIGRATED_KEY } from "./chat-store";
import {
  migrateLocalSessionsOnce,
  pushClose,
  pushRename,
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

  it("pushes a rename and a close to the server", () => {
    pushRename("chat-r", "Renamed");
    expect(api.updateSession).toHaveBeenCalledWith("chat-r", { title: "Renamed" });
    pushClose("chat-c");
    expect(api.updateSession).toHaveBeenCalledWith("chat-c", { closed: true });
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
