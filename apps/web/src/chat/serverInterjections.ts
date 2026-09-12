// What becomes of an interjection once the SERVER turn it was sent to is no longer live.
//
// An attended server-fired turn (background push-resume, scheduled fire, watch reaction —
// #3092) takes interjections through its durable control task: the server drops them into
// the session's ordinary steering queue, and whatever turn next reaches a model-call
// boundary folds them in. While the turn is live, the bus's steer-consumed frame settles
// each one at the boundary it was read (serverTurnProgress.ts). This module decides the
// rest — the cases that frame can't cover — from what the SERVER says, never from what the
// console assumes.
//
// Two server-side facts decide it, and they are not equally trustworthy:
//
//   * the steering QUEUE (`GET …/steer`) is in-memory (graph/steering.py), so "gone from
//     the queue" means SOMETHING took it — the agent, or a restart that dropped it;
//   * the durable TASK HISTORY carries the executor's `steer-consumed-v1` marker, which is
//     positive proof the agent read that exact id (verified in the wild: the marker for
//     Josh's "yes 2024 as proposed" is in jobCoach's task store).
//
// So the marker is what settles a message confidently, the queue is what says a message is
// still on its way, and neither is ever used to guess: an unresolved item is KEPT (the
// caller re-checks on a backoff) rather than settled as read or sent twice.
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

/** Successful SERVER READS before an item nobody can account for stops claiming "queued".
 *  With the caller's backoff ladder that is ~15s of actually asking — long enough for a
 *  marker, a drain record or a terminal state to show up, short enough that a bubble never
 *  claims "sent to this server turn" forever. Reads that never reached the server do not
 *  count: a blip must not spend the grace it exists to give. */
export const UNRESOLVED_ASKS = 4;
/** A submission the server never acknowledged needs fewer: if it were queued, the very next
 *  read would list it. */
export const UNCONFIRMED_ASKS = 2;

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
  /** The agent read it (or nothing can say otherwise): settle into the transcript as an
   *  ordinary user message. Never re-offer its words — a duplicate in the composer is the
   *  one outcome worse than a bubble whose exact boundary we can't name. */
  settle: QueuedSteer[];
  /** Still on its way, or not yet answerable: leave it queued and re-check. */
  keep: QueuedSteer[];
  /** Another turn will drain it: the same item, re-targeted to that turn. */
  retarget: QueuedSteer[];
  /** Nothing will drain it: pull it out of the queue and send it as a normal message. */
  resend: QueuedSteer[];
  /** The server never acknowledged this submission and can't account for it now. The caller
   *  DEQUEUES it before deciding: that is what makes handing the words back safe (a copy the
   *  server still held could otherwise be read after the operator was told it wasn't sent),
   *  and the dequeue's answer is the only thing allowed to settle it instead. */
  reclaim: QueuedSteer[];
};

export function planInterjectionReconcile(
  stale: readonly QueuedSteer[],
  ctx: {
    /** Ids still in the server's steering queue for this session. */
    pendingIds: ReadonlySet<string>;
    /** Ids the server says a turn FOLDED IN (`GET …/steer`'s `drained`). Empty from an
     *  older server, which reads as "can't say", never as "not read". */
    drainedIds: ReadonlySet<string>;
    /** Phase of each server turn an interjection was sent to. */
    phases: ReadonlyMap<string, ServerTurnPhase>;
    /** Ids each turn's DURABLE history records as folded in (steer-consumed-v1). */
    consumedIds: ReadonlyMap<string, ReadonlySet<string>>;
    /** This browser is streaming its own turn — which drains the same queue. */
    ownStreamLive: boolean;
    /** An attended server turn is live right now ("" when none). */
    liveServerTaskId: string;
    /** A HITL form is open in this chat. */
    hitlPending: boolean;
    /** Successful server reads for this unresolved episode (the caller's ladder). */
    asks: number;
  },
): InterjectionPlan {
  const plan: InterjectionPlan = { settle: [], keep: [], retarget: [], resend: [], reclaim: [] };
  for (const item of stale) {
    const task = item.serverTaskId ?? "";
    const phase = ctx.phases.get(task) ?? "unknown";
    // Proof it was read, from either record that can outlive the queue: the turn's durable
    // history, or the server's own drain log. Whatever the queue says.
    if (ctx.consumedIds.get(task)?.has(item.id) || ctx.drainedIds.has(item.id)) {
      plan.settle.push(item);
      continue;
    }
    if (ctx.pendingIds.has(item.id)) {
      // Still queued server-side. Whoever runs next folds it in, so the only question is
      // whether anything WILL run — never whether to settle it.
      if (phase === "live" || phase === "unknown") plan.keep.push(item);
      else if (ctx.ownStreamLive) plan.keep.push(item);
      else if (ctx.liveServerTaskId) plan.retarget.push({ ...item, serverTaskId: ctx.liveServerTaskId });
      else if (phase === "paused" || ctx.hitlPending) plan.keep.push(item);
      else plan.resend.push(item);
      continue;
    }
    // Gone from the queue, and nothing says the agent read it.
    if (item.unconfirmed) {
      // The POST never answered, so the console doesn't even know the server took it.
      // Queued submissions show up in the read above and read ones in `drained`, so an
      // absence from both says it never landed — but that is resolved by DEQUEUEING it
      // (reclaim), not by assuming. Never on the first look, whatever the task state: a
      // submission still in flight across a reload lands in the queue moments later, and
      // the re-check sees it there instead of telling the operator it was never sent.
      if (ctx.asks >= UNCONFIRMED_ASKS) plan.reclaim.push(item);
      else plan.keep.push(item);
    } else if (phase === "ended" || ctx.asks >= UNRESOLVED_ASKS) {
      // Acknowledged, then gone from the queue, with no record of being read. Something
      // took it: the live marker is best-effort and the drain log doesn't outlive a
      // restart, so "delivered but unlocated" is the honest reading — settle it where the
      // fallback puts it. Its words are NOT re-offered: an operator re-sending a message
      // the agent already used is worse than a bubble whose exact boundary we can't name,
      // which is the same call the re-send path makes.
      plan.settle.push(item);
    } else {
      plan.keep.push(item);
    }
  }
  return plan;
}
