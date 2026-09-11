// What becomes of an interjection once the SERVER turn it was sent to is no longer live.
//
// An attended server-fired turn (background push-resume, scheduled fire, watch reaction —
// #3092) takes interjections through its durable control task: the server drops them into
// the session's ordinary steering queue, and whatever turn next reaches a model-call
// boundary folds them in. While the turn is live, the bus's steer-consumed frame settles
// each one at the boundary it was read (serverTurnProgress.ts). This module decides the
// rest — the cases that frame can't cover — from what the SERVER says, never from what the
// console assumes:
//
//   - gone from the server's queue  → the agent consumed it (the frame was missed across an
//     SSE reconnect — progress frames are live-only). Settle it into the transcript.
//   - still queued, turn still running (or unknowable) → leave it; the turn may yet reach it.
//   - still queued, and another turn is live → that turn will drain it. Re-label it for that
//     turn instead of pulling it out from under the agent.
//   - still queued, turn parked on a HITL form → it folds in after the form answer, the same
//     as a steer queued behind a form (#1560). Keep it queued as a plain steer.
//   - still queued, turn over, nothing live → nothing will EVER drain it at the right time.
//     Leaving it in the queue delivers it, unseen, to whatever turn runs next; dropping the
//     bubble (the old behaviour) hid that. Send it as the operator's next message instead.
//
// Pure, so the whole table is unit-tested (serverInterjections.test.ts); ChatSurface does
// the RPCs and applies the plan.

import type { QueuedSteer } from "../lib/types";

/** Where the server turn an interjection was sent to stands, per its durable task. */
export type ServerTurnPhase = "live" | "paused" | "ended" | "unknown";

// Kept in step with reattach.ts / streamWatchdog.ts.
const TERMINAL = /completed|failed|canceled|cancelled|rejected/i;
const PAUSED = /input.required|auth.required/i;

/** Classify a task state from `GetTask`. Empty means the read told us nothing. */
export function serverTurnPhase(state: string): ServerTurnPhase {
  if (!state) return "unknown";
  if (PAUSED.test(state)) return "paused";
  if (TERMINAL.test(state)) return "ended";
  return "live";
}

/** Interjections sent to a server turn that is not the live one now — the reconcile set.
 *  An interjection whose POST is still in flight is never in it: the server hasn't answered
 *  whether it was even queued, so "absent from the queue" would read as "consumed". */
export function staleInterjections(
  queue: readonly QueuedSteer[],
  liveServerTaskId: string,
  inFlight: ReadonlySet<string>,
): QueuedSteer[] {
  return queue.filter(
    (item) => item.serverTaskId && item.serverTaskId !== liveServerTaskId && !inFlight.has(item.id),
  );
}

export type InterjectionPlan = {
  /** No longer in the server's queue: the agent consumed it. Settle into the transcript. */
  settle: QueuedSteer[];
  /** Still queued and its turn may still reach it (or we can't tell): leave it alone. */
  keep: QueuedSteer[];
  /** Still queued, and another turn will drain it: the same item, re-targeted. */
  retarget: QueuedSteer[];
  /** Still queued, and nothing will drain it: pull it out and send it as a normal message. */
  resend: QueuedSteer[];
};

export function planInterjectionReconcile(
  stale: readonly QueuedSteer[],
  ctx: {
    /** Ids still in the server's steering queue for this session. */
    pendingIds: ReadonlySet<string>;
    /** Phase of each server turn a still-queued interjection was sent to. */
    phases: ReadonlyMap<string, ServerTurnPhase>;
    /** This browser is streaming its own turn — which drains the same queue. */
    ownStreamLive: boolean;
    /** An attended server turn is live right now ("" when none). */
    liveServerTaskId: string;
    /** A HITL form is open in this chat. */
    hitlPending: boolean;
  },
): InterjectionPlan {
  const plan: InterjectionPlan = { settle: [], keep: [], retarget: [], resend: [] };
  for (const item of stale) {
    if (!ctx.pendingIds.has(item.id)) {
      plan.settle.push(item);
      continue;
    }
    const phase = ctx.phases.get(item.serverTaskId ?? "") ?? "unknown";
    if (phase === "live" || phase === "unknown") {
      plan.keep.push(item);
    } else if (ctx.ownStreamLive) {
      plan.retarget.push({ id: item.id, text: item.text });
    } else if (ctx.liveServerTaskId) {
      plan.retarget.push({ ...item, serverTaskId: ctx.liveServerTaskId });
    } else if (phase === "paused" || ctx.hitlPending) {
      plan.retarget.push({ id: item.id, text: item.text });
    } else {
      plan.resend.push(item);
    }
  }
  return plan;
}
