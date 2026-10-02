// Finished-goal recency — which terminal goals the live surfaces (the Work overview card's
// "Recent" list, a chat tab's GoalRunStrip) keep on screen, and the one-line outcome they
// show. Shared by both so a goal reads and expires identically in each.

import type { GoalState } from "../lib/types";

/** How long a finished goal stays on the overview card's "Recent" list (and on its chat
 *  tab's goal strip). A goal going green is the payoff of the whole loop — it must not
 *  vanish the moment it lands — but the card is a live roll-up, not a history (the Goals
 *  panel keeps every goal). Dismissing one hides it sooner. */
export const RECENT_GOAL_WINDOW_S = 30 * 60;
/** At most this many finished goals on the card. */
export const RECENT_GOAL_LIMIT = 3;

/** A finished goal's dismiss key — session + finish time, so a goal that is restarted and
 *  finishes AGAIN shows again instead of staying hidden by the earlier dismissal. */
export function goalDismissKey(goal: GoalState): string {
  return `${goal.session_id}:${goal.finished_at ?? ""}`;
}

/** Goals that finished (achieved / exhausted / unachievable) inside the recent window,
 *  newest first, minus dismissed ones, capped at `RECENT_GOAL_LIMIT`. `now` is epoch ms. */
export function recentGoals(
  goals: GoalState[],
  now: number = Date.now(),
  dismissed: ReadonlySet<string> = new Set(),
): GoalState[] {
  const cutoff = now / 1000 - RECENT_GOAL_WINDOW_S;
  return goals
    .filter((g) => g.status !== "active" && g.finished_at != null && g.finished_at >= cutoff)
    .filter((g) => !dismissed.has(goalDismissKey(g)))
    .sort((a, b) => (b.finished_at ?? 0) - (a.finished_at ?? 0))
    .slice(0, RECENT_GOAL_LIMIT);
}

/** "just now" / "4m ago" / "2h ago" for an epoch-seconds instant. */
export function agoLabel(epochS: number | null | undefined, now: number = Date.now()): string {
  if (epochS == null) return "";
  const s = Math.max(0, now / 1000 - epochS);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/** The meta line under a finished goal: `achieved · command: pytest -q · 4m ago` — the
 *  outcome, what decided it (the verifier for a success, the reason for a failure), and when.
 *  `withStatus: false` drops the leading outcome for a surface whose title already says it. */
export function goalOutcomeLine(
  goal: GoalState,
  verifier: string,
  now: number = Date.now(),
  withStatus = true,
): string {
  const why = goal.status === "achieved" ? verifier : (goal.last_reason ?? "").trim() || verifier;
  return [withStatus ? goal.status : "", why, agoLabel(goal.finished_at, now)].filter(Boolean).join(" · ");
}
