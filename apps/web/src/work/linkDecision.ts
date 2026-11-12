/**
 * Interim directive → decision link (ADR 0129 §3.1).
 *
 * Until ADR 0129 D4's server-recorded approvals land, the console cannot read which decision an
 * approval directive came from — the wire `DirectiveRow` carries no decision id. This one pure
 * function stands in with a bounded heuristic: a directive is treated as an APPROVAL of a decision
 * when they share a session, the decision is `answered`, and the answer landed within two minutes
 * BEFORE the directive was recorded. When several answers qualify, the nearest one wins. Anything
 * outside that window stays a plain instruction — the rule never guesses.
 *
 * It is deliberately the single seam the UI reads: when the server field arrives, this function
 * reads it instead and the heuristic below is deleted (its callers do not change). Real-data answer
 * timing is reliable only once bd-393xp (protoAgent#5099) stamps `answered_at` at interrupt-consume
 * time rather than at resumed-turn end — that fix is merged; this keeps unit coverage meanwhile.
 */
import { parseInstant } from "../lib/format";
import type { DirectiveRow } from "../lib/api/chat";
import type { DecisionRow } from "../lib/api/decisions";

/** The answer must land at most this long before the directive was recorded (ADR 0129 §3.1). */
export const LINK_WINDOW_MS = 120_000;

/**
 * The answered decision a directive approves, or null when none qualifies.
 *
 * A candidate must be in the SAME session, `answered`, carry a parseable `answered_at`, and sit
 * `0 ≤ directive.created_at − decision.answered_at ≤ 120s`. The nearest answer (smallest
 * non-negative gap) wins; ties keep the first in input order. A missing or invalid `answered_at`
 * (or `created_at`) never links.
 */
export function linkDecision(
  directive: DirectiveRow,
  decisions: readonly DecisionRow[],
): DecisionRow | null {
  const created = parseInstant(directive.created_at);
  if (created === null) return null;
  let best: DecisionRow | null = null;
  let bestGap = Infinity;
  for (const decision of decisions) {
    if (decision.state !== "answered") continue;
    if (decision.session_id !== directive.session_id) continue;
    const answered = parseInstant(decision.answered_at);
    if (answered === null) continue;
    const gap = created - answered;
    // Answer strictly after the directive (gap < 0) or more than the window before it never links.
    if (gap < 0 || gap > LINK_WINDOW_MS) continue;
    if (gap < bestGap) {
      best = decision;
      bestGap = gap;
    }
  }
  return best;
}
