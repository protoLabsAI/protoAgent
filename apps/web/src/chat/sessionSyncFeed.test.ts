// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The S3 console sync client (ADR 0130 D3): the change feed routes to entity refetches, and
// `resync_required` to a full refetch. Events are mocked so the handlers can be driven
// directly — the transport (EventSource) is lib/events.ts's own concern.
const { handlers } = vi.hoisted(() => ({ handlers: new Map<string, () => void>() }));
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: () => void) => {
    handlers.set(topic, fn);
    return () => handlers.delete(topic);
  },
}));

import { api, type DurableChatSession, type DurableChatTurn } from "../lib/api";
import { chatStore, type ChatSession } from "./chat-store";
import {
  hydrateDurableChatSessions,
  onServerChange,
  resetDurableResyncForTests,
  resyncDurableChatSessions,
  watchServerChangeFeed,
} from "./sessionHydration";

function summary(id: string, extra: Partial<DurableChatSession> = {}): DurableChatSession {
  return { session_id: id, last_updated: "2026-11-12T00:00:00Z", turn_count: 1, ...extra };
}
function turn(taskId: string): DurableChatTurn {
  return {
    task_id: taskId,
    state: "TASK_STATE_COMPLETED",
    last_updated: "2026-11-12T00:00:00Z",
    text: "answer",
    status: { state: "TASK_STATE_COMPLETED" },
    artifacts: [{ parts: [{ text: "answer" }] }],
    history: [{ role: "ROLE_USER", parts: [{ text: "hi" }] }],
  };
}
/** A locally-held session with one settled turn for `taskId` — in step with a server index
 *  whose newest task id and count match it (so only the backstop / `full` can flag it). */
function sessionWith(id: string, taskId: string): ChatSession {
  return {
    id,
    title: "chat",
    createdAt: 1,
    updatedAt: 1,
    messages: [
      { id: "u", role: "user", content: "hi", status: "done" },
      { id: "a", role: "assistant", content: "answer", status: "done", taskId },
    ],
  } as ChatSession;
}

/** Make `chatStore` present one in-step local session, with its reconcile/hydrate commits
 *  stubbed so a refetch is observable purely by the turns read it fans out. */
function stubStore(local: ChatSession) {
  vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ sessions: [local] } as never);
  vi.spyOn(chatStore, "captureHydrationEligibility").mockImplementation((id, opts) =>
    id === local.id ? { sessionId: id, localSession: local, behind: !!opts?.behind } : null,
  );
  vi.spyOn(chatStore, "reconcileSessions").mockImplementation(() => {});
  vi.spyOn(chatStore, "hydrateSessions").mockImplementation(() => {});
}

describe("the console sync client (ADR 0130 D3)", () => {
  beforeEach(() => {
    resetDurableResyncForTests();
    handlers.clear();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    resetDurableResyncForTests();
  });

  it("subscribes to the change-feed topics and resync_required", () => {
    watchServerChangeFeed();
    expect([...handlers.keys()].sort()).toEqual(
      [
        "resync_required",
        "session.changed",
        "turn.completed",
        "turn.failed",
        "turn.finished",
        "turn.resumed",
        "turn.started",
        // ADR 0130 S6 session-LIST events — the tab list follows the server.
        "session.created",
        "session.renamed",
        "session.reopened",
        "session.closed",
        "session.deleted",
      ].sort(),
    );
  });

  it("a change-feed frame refetches the session index (entity refetch, D2)", async () => {
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ sessions: [] } as never);
    vi.spyOn(chatStore, "hydrateSessions").mockImplementation(() => {});
    const index = vi.spyOn(api, "chatSessions").mockResolvedValue({ sessions: [] });
    vi.spyOn(api, "chatSessionTurns").mockResolvedValue({ turns: [] });

    watchServerChangeFeed();
    handlers.get("session.changed")!();
    await vi.waitFor(() => expect(index).toHaveBeenCalled());
  });

  it("coalesces a synchronous burst of frames into two resyncs, not three", async () => {
    vi.spyOn(chatStore, "getSnapshot").mockReturnValue({ sessions: [] } as never);
    vi.spyOn(chatStore, "hydrateSessions").mockImplementation(() => {});
    // Each hydrate reads the index twice (newest + parked), so N resyncs = 2N calls.
    const index = vi.spyOn(api, "chatSessions").mockResolvedValue({ sessions: [] });
    vi.spyOn(api, "chatSessionTurns").mockResolvedValue({ turns: [] });

    // The burst is synchronous: the first frame's resync is in flight by the time the next
    // two land, so they collapse into exactly one more resync — two in all, not three.
    onServerChange();
    onServerChange();
    onServerChange();
    await vi.waitFor(() => expect(index.mock.calls.length).toBe(4));
    await new Promise((r) => setTimeout(r, 5)); // a stray third resync would show here
    expect(index.mock.calls.length).toBe(4);
  });

  it("resync_required forces a full refetch of an in-step session's turns (D3)", async () => {
    const local = sessionWith("chat-full", "task-y");
    stubStore(local);
    vi.spyOn(api, "chatSessions").mockResolvedValue({
      sessions: [summary(local.id, { last_task_id: "task-y" })],
    });
    const reads = vi.spyOn(api, "chatSessionTurns").mockResolvedValue({ turns: [turn("task-y")], live_task_id: null });

    watchServerChangeFeed();
    handlers.get("resync_required")!();
    await vi.waitFor(() => expect(reads.mock.calls.map(([id]) => id)).toEqual([local.id]));
  });

  it("a resync_required arriving during an EXTERNAL resync still forces the full refetch (not dropped)", async () => {
    const local = sessionWith("chat-ext", "task-z");
    stubStore(local);
    // The external resync (boot force:true / visibility — OUTSIDE the coalescer) hangs on its
    // index read, so it is still in flight when the frame lands.
    let releaseIndex!: () => void;
    const indexGate = new Promise<void>((r) => (releaseIndex = r));
    vi.spyOn(api, "chatSessions").mockImplementation(async () => {
      await indexGate;
      return { sessions: [summary(local.id, { last_task_id: "task-z" })] };
    });
    const reads = vi.spyOn(api, "chatSessionTurns").mockResolvedValue({ turns: [turn("task-z")], live_task_id: null });

    watchServerChangeFeed();
    const external = resyncDurableChatSessions({ force: true }); // non-full, outside the coalescer
    handlers.get("resync_required")!(); // onServerChange({ full: true }) while the external one runs

    releaseIndex();
    await external; // the external (non-full, in-step) resync reads no turns
    // Gating onServerChange on resyncInFlight used to drop this; the drain now waits the external
    // resync out and runs a FRESH full resync, which re-reads the in-step session's turns.
    await vi.waitFor(() => expect(reads.mock.calls.map(([id]) => id)).toEqual([local.id]));
  });

  it("the last_change_seq backstop refetches an in-step session only once its server seq climbs", async () => {
    const local = sessionWith("chat-seq", "task-x");
    stubStore(local);
    const index = vi.spyOn(api, "chatSessions");
    const reads = vi.spyOn(api, "chatSessionTurns").mockResolvedValue({ turns: [turn("task-x")], live_task_id: null });

    // First sight at seq 10: recorded, not treated as behind (boot already hydrated it).
    index.mockResolvedValue({ sessions: [summary(local.id, { last_task_id: "task-x", last_change_seq: 10 })] });
    await hydrateDurableChatSessions();
    expect(reads).not.toHaveBeenCalled();

    // The server's seq climbs with no new turn (a rename / status flip) → refetch.
    index.mockResolvedValue({ sessions: [summary(local.id, { last_task_id: "task-x", last_change_seq: 20 })] });
    await hydrateDurableChatSessions();
    expect(reads.mock.calls.map(([id]) => id)).toEqual([local.id]);
  });
});
