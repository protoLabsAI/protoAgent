// The ONE owner of "start a goal's kickoff turn" in a chat slot (ADR 0090 D1).
//
// `/goal new` and the Work-panel flow set a goal `kick:false` and queue a hidden kickoff on
// the chat-store seam (`registerGoalKickoff`); the tab's slot then runs it as a turn so the
// drive loop streams live there. A slot must never run two turns at once, and the kickoff
// must neither be dropped nor run twice, so it fires only when the tab is truly free:
//
//   1. the session doesn't read "streaming";
//   2. no local turn (runTurn) is in flight — idle is set INSIDE runTurn, whose `finally`
//      still resets abortRef / the watchdog; starting the next turn there clobbers them;
//   3. no turn is parked on the operator (a paused bubble: ask_human / form / approval) —
//      a new turn would abandon the pending interrupt;
//   4. the slot's own `blocked()` says nothing else owns the next turn — the steer queue
//      (a queued message the turn-end reconcile will re-send as a fresh turn) and the HITL
//      panel.
//
// Otherwise it stays queued and is re-checked on every kickoff registration, every store
// change, and every `poke()` (the slot pokes after a turn's `finally` and after its steer
// reconcile settles). Each check is posted to a macrotask and re-evaluated there.
// `takeGoalKickoff` removes it as it fires, so it runs exactly once.

import {
  chatStore,
  hasGoalKickoff,
  isParkedSession,
  subscribeGoalKickoff,
  takeGoalKickoff,
} from "./chat-store";
import { localTurnInFlight } from "./sessionLiveness";

export interface GoalKickoffWatch {
  /** Re-check now (posted to a macrotask) — call when something the gate reads changed. */
  poke: () => void;
  /** Stop watching. A still-pending kickoff stays queued for the next watcher. */
  stop: () => void;
}

/** Whether the session itself is free to start a turn (gates 1–3). */
export function sessionFreeForKickoff(sessionId: string): boolean {
  const snap = chatStore.getSnapshot();
  if (snap.sessionStatusMap[sessionId] === "streaming") return false;
  if (localTurnInFlight(sessionId)) return false;
  const session = snap.sessions.find((s) => s.id === sessionId);
  return !(session && isParkedSession(session));
}

export function watchGoalKickoff(
  sessionId: string,
  run: (prompt: string) => void,
  blocked: () => boolean = () => false,
): GoalKickoffWatch {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;
  const fire = () => {
    timer = null;
    if (stopped || !hasGoalKickoff(sessionId)) return;
    if (!sessionFreeForKickoff(sessionId) || blocked()) return; // a later trigger re-checks
    const prompt = takeGoalKickoff(sessionId);
    if (prompt) run(prompt);
  };
  const poke = () => {
    if (stopped || timer !== null || !hasGoalKickoff(sessionId)) return;
    timer = setTimeout(fire, 0);
  };
  const offKickoff = subscribeGoalKickoff(poke);
  const offStore = chatStore.subscribe(poke);
  poke();
  return {
    poke,
    stop: () => {
      stopped = true;
      offKickoff();
      offStore();
      if (timer !== null) clearTimeout(timer);
    },
  };
}
