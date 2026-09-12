import { describe, expect, it } from "vitest";

import type { QueuedSteer } from "../lib/types";
import {
  planInterjectionReconcile,
  serverTurnPhase,
  staleInterjections,
  UNCONFIRMED_ASKS,
  UNRESOLVED_ASKS,
  type ServerTurnPhase,
} from "./serverInterjections";

const toX = (id: string, extra: Partial<QueuedSteer> = {}): QueuedSteer => ({
  id,
  text: `msg ${id}`,
  serverTaskId: "task-x",
  ...extra,
});

function plan(
  stale: QueuedSteer[],
  over: Partial<{
    pending: string[];
    drained: string[];
    phase: ServerTurnPhase;
    consumed: string[];
    ownStreamLive: boolean;
    liveServerTaskId: string;
    hitlPending: boolean;
    asks: number;
  }> = {},
) {
  return planInterjectionReconcile(stale, {
    pendingIds: new Set(over.pending ?? []),
    drainedIds: new Set(over.drained ?? []),
    phases: new Map([["task-x", over.phase ?? "ended"]]),
    consumedIds: new Map([["task-x", new Set(over.consumed ?? [])]]),
    ownStreamLive: over.ownStreamLive ?? false,
    liveServerTaskId: over.liveServerTaskId ?? "",
    hitlPending: over.hitlPending ?? false,
    asks: over.asks ?? 0,
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

  it("never reads an in-flight submission's absence as consumed — it waits for the answer", () => {
    expect(staleInterjections(queue, "", new Set(["x1", "x2", "x3"]))).toEqual([]);
  });

  it("never touches a plain steer — its own stream's reconcile owns it", () => {
    expect(staleInterjections([queue[0]], "", new Set())).toEqual([]);
  });
});

describe("planInterjectionReconcile", () => {
  it("settles on the DURABLE marker, whatever the queue says", () => {
    // The steering queue is in-memory; the task history is the record that survives a
    // restart — and it names the exact id the agent read.
    expect(plan([toX("a")], { consumed: ["a"], phase: "live", pending: ["a"] }).settle).toEqual([toX("a")]);
  });

  it("settles a confirmed message that left the queue of a turn that is over", () => {
    expect(plan([toX("a")], { pending: [] }).settle).toEqual([toX("a")]);
  });

  it("never re-sends or re-offers a message that left the queue — a duplicate is worse", () => {
    // Both failure modes of guessing: sending it again (the agent reads it twice) and
    // putting the words back in the composer (the operator sends it again).
    const out = plan([toX("a")], { pending: [] });
    expect(out.resend).toEqual([]);
    expect(out.reclaim).toEqual([]);
    expect(out.settle).toEqual([toX("a")]);
  });

  it("leaves a still-queued interjection alone while its turn is running — or unknowable", () => {
    for (const phase of ["live", "unknown"] as const) {
      expect(plan([toX("a")], { pending: ["a"], phase }).keep).toEqual([toX("a")]);
    }
  });

  it("re-sends a still-queued interjection once its turn is over and nothing else will drain it", () => {
    expect(plan([toX("a")], { pending: ["a"], phase: "ended" }).resend).toEqual([toX("a")]);
  });

  it("hands a leftover to another live SERVER turn instead of pulling it out from under it", () => {
    expect(plan([toX("a")], { pending: ["a"], liveServerTaskId: "task-y" }).retarget).toEqual([
      { id: "a", text: "msg a", serverTaskId: "task-y" },
    ]);
  });

  it("leaves a leftover to this browser's own live stream, which drains the same queue", () => {
    const out = plan([toX("a")], { pending: ["a"], ownStreamLive: true });
    expect(out.keep).toEqual([toX("a")]);
    expect(out.resend).toEqual([]);
  });

  it("keeps a leftover queued behind a HITL form — it folds in after the answer (#1560)", () => {
    // Kept TAGGED, not downgraded to a plain steer: this reconcile stays its owner, so an
    // approval answered on another device (which drains the queue here) still retires the
    // bubble on the next re-check instead of stranding it until the operator's next turn.
    for (const over of [{ phase: "paused" as const }, { hitlPending: true }]) {
      const out = plan([toX("a")], { pending: ["a"], ...over });
      expect(out.keep).toEqual([toX("a")]);
      expect(out.resend).toEqual([]);
    }
  });

  it("waits, then RECLAIMS a submission the server never acknowledged", () => {
    // Unconfirmed + absent from both the queue and the drain log = the POST probably never
    // landed. It is not settled (that would claim the agent read words it never saw) and
    // not handed back on trust either: the caller dequeues it first, which is what makes
    // giving the words back safe.
    const item = toX("a", { unconfirmed: true });
    expect(plan([item], { pending: [], phase: "live" }).keep).toEqual([item]);
    expect(plan([item], { pending: [], phase: "live", asks: UNCONFIRMED_ASKS }).reclaim).toEqual([item]);
    // Not even when the turn is already over: a submission still in flight across a reload
    // lands in the queue moments later, and the re-check must get the chance to see it
    // there. Handing the words back on the first look told operators a message wasn't sent
    // while the server was about to feed it to the agent (R9).
    expect(plan([item], { pending: [] }).keep).toEqual([item]);
    expect(plan([item], { pending: [], asks: UNCONFIRMED_ASKS }).reclaim).toEqual([item]);
    // Confirmed by presence in the queue: the POST did land, so the ordinary rules apply.
    expect(plan([item], { pending: ["a"], phase: "live" }).keep).toEqual([item]);
    // …and the drain log answers it outright: the agent read it.
    expect(plan([item], { drained: ["a"], pending: [], asks: UNCONFIRMED_ASKS }).settle).toEqual([item]);
  });

  it("settles — never re-offers — an acknowledged item nothing can account for", () => {
    // Not queued, no marker, no drain record, and the task never reached a terminal state
    // (the shape a restart mid-turn leaves). The live marker is best-effort and the drain
    // log doesn't outlive a restart, so "delivered but unlocated" is the honest reading:
    // kept while that could still resolve, then settled. Its words are never re-offered —
    // an operator re-sending a message the agent already used is the worse outcome.
    expect(plan([toX("a")], { pending: [], phase: "live" }).keep).toEqual([toX("a")]);
    const out = plan([toX("a")], { pending: [], phase: "live", asks: UNRESOLVED_ASKS });
    expect(out.settle).toEqual([toX("a")]);
    expect(out.reclaim).toEqual([]);
  });

  it("settles on the server's drain log even while the turn is still running", () => {
    // The producer of the live boundary marker is best-effort — the sync middleware path
    // and a failed dispatch both emit nothing — so the drain log is what keeps a read
    // message from sitting queued under the answer that used it.
    expect(plan([toX("a")], { drained: ["a"], pending: [], phase: "live" }).settle).toEqual([toX("a")]);
  });

  it("splits a mixed batch item by item, in queue order", () => {
    const out = plan([toX("a"), toX("b"), toX("c")], { pending: ["b"], consumed: ["c"] });
    expect(out.settle.map((q) => q.id)).toEqual(["a", "c"]);
    expect(out.resend.map((q) => q.id)).toEqual(["b"]);
  });
});
