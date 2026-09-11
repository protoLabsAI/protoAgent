import { describe, expect, it } from "vitest";

import type { QueuedSteer } from "../lib/types";
import {
  planInterjectionReconcile,
  serverTurnPhase,
  staleInterjections,
  type ServerTurnPhase,
} from "./serverInterjections";

const toX = (id: string, text = `msg ${id}`): QueuedSteer => ({ id, text, serverTaskId: "task-x" });

function plan(
  stale: QueuedSteer[],
  over: Partial<{
    pending: string[];
    phase: ServerTurnPhase;
    ownStreamLive: boolean;
    liveServerTaskId: string;
    hitlPending: boolean;
  }> = {},
) {
  return planInterjectionReconcile(stale, {
    pendingIds: new Set(over.pending ?? []),
    phases: new Map([["task-x", over.phase ?? "ended"]]),
    ownStreamLive: over.ownStreamLive ?? false,
    liveServerTaskId: over.liveServerTaskId ?? "",
    hitlPending: over.hitlPending ?? false,
  });
}

describe("serverTurnPhase", () => {
  it("reads the durable task state the way reattach does", () => {
    expect(serverTurnPhase("TASK_STATE_WORKING")).toBe("live");
    expect(serverTurnPhase("TASK_STATE_SUBMITTED")).toBe("live");
    expect(serverTurnPhase("TASK_STATE_INPUT_REQUIRED")).toBe("paused");
    expect(serverTurnPhase("TASK_STATE_AUTH_REQUIRED")).toBe("paused");
    expect(serverTurnPhase("TASK_STATE_COMPLETED")).toBe("ended");
    expect(serverTurnPhase("TASK_STATE_FAILED")).toBe("ended");
    expect(serverTurnPhase("TASK_STATE_CANCELED")).toBe("ended");
    expect(serverTurnPhase("")).toBe("unknown");
  });
});

describe("staleInterjections", () => {
  const queue: QueuedSteer[] = [
    { id: "steer", text: "a steer into this browser's own stream" },
    { id: "x1", text: "to the live turn", serverTaskId: "task-live" },
    { id: "x2", text: "to a turn that ended", serverTaskId: "task-old" },
    { id: "x3", text: "posted, not yet answered", serverTaskId: "task-old" },
  ];

  it("selects interjections sent to a server turn that is not the live one", () => {
    expect(staleInterjections(queue, "task-live", new Set(["x3"])).map((q) => q.id)).toEqual(["x2"]);
  });

  it("treats every server-turn interjection as stale when no server turn is live", () => {
    expect(staleInterjections(queue, "", new Set()).map((q) => q.id)).toEqual(["x1", "x2", "x3"]);
  });

  it("never touches a plain steer — its own stream's reconcile owns it", () => {
    expect(staleInterjections([queue[0]], "", new Set())).toEqual([]);
  });
});

describe("planInterjectionReconcile", () => {
  it("settles what the server no longer holds: the agent consumed it (a missed marker)", () => {
    expect(plan([toX("a")], { pending: [] })).toEqual({ settle: [toX("a")], keep: [], retarget: [], resend: [] });
  });

  it("leaves a still-queued interjection alone while its turn is running — or unknowable", () => {
    for (const phase of ["live", "unknown"] as const) {
      expect(plan([toX("a")], { pending: ["a"], phase }).keep).toEqual([toX("a")]);
    }
  });

  it("re-sends a still-queued interjection once its turn is over and nothing else will drain it", () => {
    expect(plan([toX("a")], { pending: ["a"], phase: "ended" }).resend).toEqual([toX("a")]);
  });

  it("hands a leftover to the turn that is live now instead of pulling it out from under it", () => {
    // Another server turn: it drains the same queue at its next model call.
    expect(plan([toX("a")], { pending: ["a"], liveServerTaskId: "task-y" }).retarget).toEqual([
      { id: "a", text: "msg a", serverTaskId: "task-y" },
    ]);
    // This browser's own stream: it becomes a plain steer, settled by that stream.
    expect(plan([toX("a")], { pending: ["a"], ownStreamLive: true }).retarget).toEqual([{ id: "a", text: "msg a" }]);
  });

  it("keeps a leftover queued behind a HITL form — it folds in after the answer (#1560)", () => {
    expect(plan([toX("a")], { pending: ["a"], phase: "paused" }).retarget).toEqual([{ id: "a", text: "msg a" }]);
    expect(plan([toX("a")], { pending: ["a"], hitlPending: true }).retarget).toEqual([{ id: "a", text: "msg a" }]);
  });

  it("splits a mixed batch item by item, in queue order", () => {
    const out = plan([toX("a"), toX("b"), toX("c")], { pending: ["b"] });
    expect(out.settle.map((q) => q.id)).toEqual(["a", "c"]);
    expect(out.resend.map((q) => q.id)).toEqual(["b"]);
  });
});
