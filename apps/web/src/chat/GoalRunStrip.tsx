import "../goals/goal-status.css";

import { Button } from "@protolabsai/ui/primitives";
import { useQuery } from "@tanstack/react-query";
import { CircleCheck, X } from "lucide-react";

import { goalDismissKey, goalOutcomeLine, RECENT_GOAL_WINDOW_S } from "../goals/recentGoals";
import { dismissGoal, useDismissedGoals } from "../goals/dismissedGoals";
import { useNow } from "../goals/useNow";
import { goalsQuery } from "../lib/queries";
import type { GoalState } from "../lib/types";
import { openView } from "../app/palette/nav";
import { useUI } from "../state/uiStore";
import { endThisChatVisit } from "../work/visitScope";
import { verifierLabel } from "./goalForm";

/** Which goal (if any) this chat's strip shows, and how: the session's goal while it drives,
 *  or once it finished inside the recent window and wasn't dismissed. Pure, for the tests. */
export function goalStripState(
  goals: GoalState[] | undefined,
  sessionId: string,
  now: number,
  dismissed: ReadonlySet<string>,
): { goal: GoalState; phase: "driving" | "achieved" | "failed" } | null {
  const goal = goals?.find((g) => g.session_id === sessionId);
  if (!goal) return null;
  if (goal.status === "active" && !goal.finished_at) return { goal, phase: "driving" };
  if (goal.finished_at == null || goal.finished_at < now / 1000 - RECENT_GOAL_WINDOW_S) return null;
  if (dismissed.has(goalDismissKey(goal))) return null;
  return { goal, phase: goal.status === "achieved" ? "achieved" : "failed" };
}

/** The dismissible "Goal achieved" line (work consolidation v2 S7). The rail's Doing-now
 *  chip carries the DRIVING goal and its Needs-you section carries a STOPPED one (Keep going
 *  / Drop the goal), so this strip keeps only the payoff: a goal going green must never
 *  silently disappear — the line stays for the recent window (or until dismissed), live off
 *  the goal bus events that keep the goals cache fresh (App.tsx). Renders nothing otherwise. */
export function GoalRunStrip({ sessionId }: { sessionId: string }) {
  const { data } = useQuery(goalsQuery());
  const now = useNow();
  const dismissed = useDismissedGoals();
  const state = goalStripState(data?.goals, sessionId, now, dismissed);
  // Driving and stopped goals live on the chat status rail now; only "achieved" renders.
  if (!state || state.phase !== "achieved") return null;
  const { goal } = state;
  const verifier = verifierLabel(goal.verifier);
  const openWork = () => {
    const ui = useUI.getState();
    ui.setSurfaceState("work", {
      ...ui.surfaceStates.work,
      section: "goals",
      itemId: `goal:${goal.session_id}`,
      scrollTop: 0,
      filters: ui.surfaceStates.work?.filters,
    });
    // This entry keeps the saved filters — a live rail deep-link visit scope must not leak in.
    endThisChatVisit();
    openView("work");
  };
  // The live region is ONLY the state text (`Goal achieved`): the outcome line carries a
  // relative time ("3m ago") that `useNow` re-renders every minute, and a role="status"
  // around it would re-announce the whole strip to screen readers each tick.
  const line = goalOutcomeLine(goal, verifier, now, false); // the title already says "achieved"
  return (
    <div
      className="chat-goal-strip chat-goal-strip--achieved"
      data-testid="chat-goal-strip"
      data-status={goal.status}
    >
      <CircleCheck size={14} aria-hidden className="chat-goal-strip-icon" />
      <span className="chat-goal-strip-text" title={`${goal.condition}\n${line}`}>
        <span role="status" data-testid="chat-goal-strip-state">
          <strong>Goal achieved</strong>
        </span>{" "}
        · {goal.condition}
        <span className="chat-goal-strip-outcome"> — {line}</span>
      </span>
      <Button variant="ghost" size="sm" onClick={openWork}>View</Button>
      <Button
        variant="ghost"
        size="xs"
        icon
        type="button"
        title="Dismiss — it stays in the Goals panel"
        aria-label="Dismiss finished goal"
        onClick={() => dismissGoal(goalDismissKey(goal))}
      >
        <X size={13} />
      </Button>
    </div>
  );
}
