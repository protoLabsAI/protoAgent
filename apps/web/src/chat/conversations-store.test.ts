// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ConversationRow } from "../lib/api/chat";

// The console data layer for "Other conversations" (docs/design/other-conversations.md C1):
// list + page older, live a2a turn events, the per-device hidden state, and the client-only
// stage (`chat` vs `conversations`) kept apart from the chat store's `currentSessionId`.

const listeners: Record<string, (d: Record<string, unknown>) => void> = {};
let connListener: ((c: boolean) => void) | null = null;

// Every `listConversations` call, in order — so a test can prove the query args AND that the
// reserved stage id is never sent to the server (§4.7).
const calls: { group?: string; before?: string; limit?: number }[] = [];
let listConversations: (opts: {
  group?: string;
  before?: string;
  limit?: number;
}) => Promise<{ conversations: ConversationRow[]; reason?: string }>;

vi.mock("../lib/api", () => ({
  api: {
    listConversations: (opts: { group?: string; before?: string; limit?: number } = {}) => {
      calls.push(opts);
      return listConversations(opts);
    },
  },
}));
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (d: Record<string, unknown>) => void) => {
    listeners[topic] = fn;
    return () => delete listeners[topic];
  },
  onConnectionChange: (fn: (c: boolean) => void) => {
    connListener = fn;
    fn(true); // subscribe-time state — the store skips it so startup is one request
    return () => {
      connListener = null;
    };
  },
}));

const store = await import("./conversations-store");

const flush = () => new Promise((r) => setTimeout(r, 0));
const HIDDEN_KEY = "protoagent.conversations.hidden";

const row = (over: Partial<ConversationRow> = {}): ConversationRow => ({
  session_id: "ctx-1",
  title: "A delegation",
  origin_label: "From Claude Code",
  group: "for_you",
  last_updated: new Date().toISOString(),
  turn_count: 2,
  active: false,
  last_state: "completed",
  ...over,
});

const daysAgo = (n: number) => new Date(Date.now() - n * 24 * 60 * 60 * 1000).toISOString();

beforeEach(() => {
  calls.length = 0;
  for (const k of Object.keys(listeners)) delete listeners[k];
  connListener = null;
  localStorage.clear();
  store.__resetForTest();
  listConversations = () => Promise.resolve({ conversations: [] });
});

describe("r1: list + page older via GET /api/conversations", () => {
  it("requests the window with the capped limit and returns typed grouped rows", async () => {
    listConversations = () =>
      Promise.resolve({
        conversations: [row({ session_id: "ctx-1", group: "for_you" }), row({ session_id: "ctx-2", group: "automated" })],
      });
    store.subscribeForTest(() => {});
    await flush();

    expect(calls[0]).toEqual({ limit: 50 });
    const s = store.snapshotForTest();
    expect(s.status).toBe("ready");
    expect(s.rows.map((r) => r.session_id)).toEqual(["ctx-1", "ctx-2"]);
    expect(store.forYouRows(s).map((r) => r.session_id)).toEqual(["ctx-1"]);
    expect(store.automatedRows(s).map((r) => r.session_id)).toEqual(["ctx-2"]);
  });

  it("loadOlder pages back with before=<oldest last_updated> and appends", async () => {
    const recent = row({ session_id: "ctx-1", last_updated: daysAgo(1) });
    listConversations = () => Promise.resolve({ conversations: [recent] });
    store.subscribeForTest(() => {});
    await flush();

    const older = row({ session_id: "ctx-old", last_updated: daysAgo(30) });
    listConversations = () => Promise.resolve({ conversations: [older] });
    store.loadOlder();
    await flush();

    expect(calls[1]).toEqual({ before: recent.last_updated, limit: 50 });
    expect(store.snapshotForTest().rows.map((r) => r.session_id)).toEqual(["ctx-1", "ctx-old"]);
  });

  it("count is only the 7-day window, even after older rows are paged in", async () => {
    listConversations = () => Promise.resolve({ conversations: [row({ session_id: "ctx-1", last_updated: daysAgo(2) })] });
    store.subscribeForTest(() => {});
    await flush();

    listConversations = () => Promise.resolve({ conversations: [row({ session_id: "ctx-old", last_updated: daysAgo(20) })] });
    store.loadOlder();
    await flush();

    const s = store.snapshotForTest();
    expect(s.rows).toHaveLength(2);
    expect(store.conversationCount(s)).toBe(1); // only the in-window row counts
  });
});

describe("r2: live a2a turn events, no polling", () => {
  it("turn.started sets active and turn.finished sets active+last_state for a known row", async () => {
    listConversations = () => Promise.resolve({ conversations: [row({ session_id: "ctx-1", active: false, last_state: "working" })] });
    store.subscribeForTest(() => {});
    await flush();

    listeners["turn.started"]({ origin: "a2a", session_id: "ctx-1" });
    expect(store.snapshotForTest().rows[0].active).toBe(true);
    expect(calls).toHaveLength(1); // a known row never refetches

    listeners["turn.finished"]({ origin: "a2a", session_id: "ctx-1", state: "completed" });
    const r0 = store.snapshotForTest().rows[0];
    expect(r0.active).toBe(false);
    expect(r0.last_state).toBe("completed");
    expect(calls).toHaveLength(1);
  });

  it("an unknown context's turn.started refetches — that is how the tab first appears", async () => {
    listConversations = () => Promise.resolve({ conversations: [] });
    store.subscribeForTest(() => {});
    await flush();
    expect(store.snapshotForTest().rows).toHaveLength(0);

    listConversations = () => Promise.resolve({ conversations: [row({ session_id: "ctx-9" })] });
    listeners["turn.started"]({ origin: "a2a", session_id: "ctx-9" });
    await flush();

    expect(calls).toHaveLength(2);
    expect(store.snapshotForTest().rows.map((r) => r.session_id)).toEqual(["ctx-9"]);
  });

  it("ignores non-a2a turn events and never installs a poll timer", async () => {
    const setInterval = vi.spyOn(globalThis, "setInterval");
    listConversations = () => Promise.resolve({ conversations: [row({ session_id: "ctx-1", active: false })] });
    store.subscribeForTest(() => {});
    await flush();

    listeners["turn.started"]({ origin: "scheduler", session_id: "ctx-1" }); // not a2a
    expect(store.snapshotForTest().rows[0].active).toBe(false);
    expect(calls).toHaveLength(1); // no refetch, no poll
    expect(setInterval).not.toHaveBeenCalled();
    setInterval.mockRestore();
  });

  it("refetches on a bus reconnect (a fresh stream replays nothing)", async () => {
    listConversations = () => Promise.resolve({ conversations: [] });
    store.subscribeForTest(() => {});
    await flush();
    expect(calls).toHaveLength(1);

    connListener?.(false);
    connListener?.(true);
    await flush();
    expect(calls).toHaveLength(2);
  });
});

describe("r3: tabVisible + per-device hidden state", () => {
  it("is false while loading and once rows load it is true", async () => {
    let resolve: (v: { conversations: ConversationRow[] }) => void = () => {};
    listConversations = () => new Promise((r) => (resolve = r));
    store.subscribeForTest(() => {});
    await flush();

    expect(store.snapshotForTest().status).toBe("loading");
    expect(store.isTabVisible(store.snapshotForTest())).toBe(false); // loading → not visible

    resolve({ conversations: [row()] });
    await flush();
    expect(store.isTabVisible(store.snapshotForTest())).toBe(true);
  });

  it("is false with zero rows", async () => {
    listConversations = () => Promise.resolve({ conversations: [] });
    store.subscribeForTest(() => {});
    await flush();
    expect(store.snapshotForTest().status).toBe("ready");
    expect(store.isTabVisible(store.snapshotForTest())).toBe(false);
  });

  it("goes false after hide(), then true again when a row needs you", async () => {
    listConversations = () => Promise.resolve({ conversations: [row({ session_id: "ctx-1", last_state: "completed" })] });
    store.subscribeForTest(() => {});
    await flush();
    expect(store.isTabVisible(store.snapshotForTest())).toBe(true);

    store.hide();
    expect(store.isTabVisible(store.snapshotForTest())).toBe(false);
    expect(localStorage.getItem(HIDDEN_KEY)).toBe("1");

    // A park publishes turn.input_required (never turn.finished), flipping the row to Needs you —
    // a hidden tab must return on its own (§3.2).
    listeners["turn.input_required"]({ context_id: "ctx-1" });
    const s = store.snapshotForTest();
    expect(store.needsYouCount(s)).toBe(1);
    expect(store.isTabVisible(s)).toBe(true);
  });

  it("a deep link shows the tab even when hidden and empty (and while loading)", async () => {
    let resolve: (v: { conversations: ConversationRow[] }) => void = () => {};
    listConversations = () => new Promise((r) => (resolve = r));
    store.subscribeForTest(() => {});
    await flush();
    store.hide();
    expect(store.isTabVisible(store.snapshotForTest())).toBe(false);

    store.openDeepLink("ctx-deep");
    const s = store.snapshotForTest();
    expect(store.isTabVisible(s)).toBe(true); // forced open even while loading + hidden
    expect(s.stage).toBe("conversations");
    expect(s.openThreadId).toBe("ctx-deep");

    resolve({ conversations: [] });
    await flush();
    expect(store.isTabVisible(store.snapshotForTest())).toBe(true); // still, with zero rows
  });

  it("persists the hidden flag in localStorage across a reload", async () => {
    listConversations = () => Promise.resolve({ conversations: [row()] });
    store.subscribeForTest(() => {});
    await flush();

    store.hide();
    expect(localStorage.getItem(HIDDEN_KEY)).toBe("1");

    // A fresh store (reload) reads the device flag back — localStorage is NOT cleared here.
    store.__resetForTest();
    store.subscribeForTest(() => {});
    await flush();
    expect(store.snapshotForTest().hidden).toBe(true);
  });
});

describe("r4: the stage is separate from the session, and the reserved id never ships", () => {
  it("switches chat ↔ conversations and remembers the open thread, with no server call", async () => {
    listConversations = () => Promise.resolve({ conversations: [row({ session_id: "ctx-1" })] });
    store.subscribeForTest(() => {});
    await flush();
    expect(calls).toHaveLength(1);
    expect(store.snapshotForTest().stage).toBe("chat");

    store.openConversations();
    expect(store.snapshotForTest().stage).toBe("conversations");
    expect(store.snapshotForTest().openThreadId).toBeNull();

    store.openThread("ctx-1");
    expect(store.snapshotForTest().openThreadId).toBe("ctx-1");

    store.openChat(); // selecting a chat tab leaves, but the open thread is remembered
    expect(store.snapshotForTest().stage).toBe("chat");
    expect(store.snapshotForTest().openThreadId).toBe("ctx-1");

    // None of the stage mutations touched the server (so `currentSessionId` is never involved).
    expect(calls).toHaveLength(1);
  });

  it("never sends the reserved id to the server, and won't open it as a thread", async () => {
    listConversations = () => Promise.resolve({ conversations: [row({ session_id: "ctx-1", last_updated: daysAgo(1) })] });
    store.subscribeForTest(() => {});
    await flush();

    store.openConversations();
    store.openThread(store.CONVERSATIONS_STAGE_ID); // guarded: not a real thread
    expect(store.snapshotForTest().openThreadId).toBeNull();
    store.loadOlder();
    await flush();

    expect(store.CONVERSATIONS_STAGE_ID).toBe("conversations");
    expect(store.isConversationsStage("conversations")).toBe(true);
    expect(store.isConversationsStage("ctx-1")).toBe(false);
    // The reserved id appears in NO request — not as group, before, or any value.
    for (const c of calls) {
      expect(Object.values(c)).not.toContain("conversations");
    }
  });
});
