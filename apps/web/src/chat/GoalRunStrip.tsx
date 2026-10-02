import "../goals/goal-status.css";

import { Button } from "@protolabsai/ui/primitives";
import { Spinner } from "@protolabsai/ui/data";
import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleX, Target, X } from "lucide-react";

import { goalDismissKey, goalOutcomeLine, RECENT_GOAL_WINDOW_S } from "../goals/recentGoals";
import { dismissGoal, useDismissedGoals } from "../goals/dismissedGoals";
import { useNow } from "../goals/useNow";
import { goalsQuery } from "../lib/queries";
import type { GoalState } from "../lib/types";
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

/** The goal this chat is driving, above the composer (beside the background-work strip):
 *  `driving · iteration 2/8` with a spinner while the loop runs, then a green
 *  `Goal achieved · command: pytest -q · just now` (or the red stop reason) the moment the
 *  verifier decides — live, off the goal bus events that keep the goals cache fresh
 *  (App.tsx). The turns themselves stream in the transcript as normal turns; this is the
 *  goal's own status, which no single turn carries. Renders nothing for a chat with no goal. */
export function GoalRunStrip({ sessionId }: { sessionId: string }) {
  const { data } = useQuery(goalsQuery());
  const now = useNow();
  const dismissed = useDismissedGoals();
  const state = goalStripState(data?.goals, sessionId, now, dismissed);
  if (!state) return null;
  const { goal, phase } = state;
  const verifier = verifierLabel(goal.verifier);
  // The live region is ONLY the state text (`Goal driving · iteration i/n`, `Goal achieved`):
  // the outcome line carries a relative time ("3m ago") that `useNow` re-renders every minute,
  // and a role="status" around it would re-announce the whole strip to screen readers each tick.

  if (phase === "driving") {
    const progress = `iteration ${goal.iteration ?? 0}/${goal.max_iterations ?? "∞"}`;
    return (
      <div className="chat-goal-strip" data-testid="chat-goal-strip" data-status="active">
        <Spinner size={12} />
        <Target size={13} aria-hidden className="chat-goal-strip-icon" />
        <span className="chat-goal-strip-text" title={`${goal.condition}\n${verifier}`}>
          <span role="status" data-testid="chat-goal-strip-state">
            <strong>Goal driving</strong> · {progress}
          </span>{" "}
          · {goal.condition}
        </span>
      </div>
    );
  }

  const achieved = phase === "achieved";
  const line = goalOutcomeLine(goal, verifier, now, false); // the title already says "achieved"
  return (
    <div
      className={`chat-goal-strip chat-goal-strip--${phase}`}
      data-testid="chat-goal-strip"
      data-status={goal.status}
    >
      {achieved ? (
        <CircleCheck size={14} aria-hidden className="chat-goal-strip-icon" />
      ) : (
        <CircleX size={14} aria-hidden className="chat-goal-strip-icon" />
      )}
      <span className="chat-goal-strip-text" title={`${goal.condition}\n${line}`}>
        <span role="status" data-testid="chat-goal-strip-state">
          <strong>{achieved ? "Goal achieved" : `Goal ${goal.status}`}</strong>
        </span>{" "}
        · {goal.condition}
        <span className="chat-goal-strip-outcome"> — {line}</span>
      </span>
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
