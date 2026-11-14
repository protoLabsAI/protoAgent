// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { removeKey } from "../lib/storage";
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

  it("migrates this device's non-empty open tabs exactly once", async () => {
    const chat = chatStore.createSession();
    chatStore.updateMessages(chat.id, [userMsg("Deploy status")]);
    await migrateLocalSessionsOnce();
    expect(api.migrateSessions).toHaveBeenCalledTimes(1);
    const sent = (api.migrateSessions as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as { session_id: string }[];
    expect(sent.some((s) => s.session_id === chat.id)).toBe(true);
    // Empty, never-named placeholder tabs are not chats to keep — never migrated.
    expect(sent.some((s) => s.session_id !== chat.id && !s.session_id)).toBe(false);
    // Latched: a second boot does not push again.
    await migrateLocalSessionsOnce();
    expect(api.migrateSessions).toHaveBeenCalledTimes(1);
  });
});
