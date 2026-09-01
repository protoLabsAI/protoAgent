import type { MemoryDigestPolicy, MemorySessionDigest } from "../lib/types";

// What the agent is actually told each turn, per `context.prior_sessions`
// (ADR 0108 D9). The panel used to describe the `newest` window unconditionally,
// which read as a lie under the other two policies — `off` injects nothing at all
// (#3308). Pure copy + one decision, kept out of the component so both are testable.

export const DIGEST_POLICY_HINT: Record<MemoryDigestPolicy, string> = {
  newest:
    "The <prior_sessions> digest the agent sees each turn carries only the newest few (token-capped; background sessions excluded) — badged rows are stored but not currently in it. A listed row can still shed under the per-turn context budget; the Injections panel is what a given turn actually received.",
  relevant:
    "The digest is set to relevant: each turn lists only the sessions matching what was just asked, so there is no fixed window to badge — every summary here is reachable.",
  off: "The automatic digest is off: none of these are injected. The agent reaches them on demand with session_search and recall_session.",
};

export const NOT_IN_DIGEST_TITLE =
  "Outside the current digest window (the newest summaries under the token cap; background sessions excluded) — stored, but not in the <prior_sessions> digest the agent sees.";

export type SessionBadge = { tone: "neutral" | "warning"; label: string; title: string };

/** The badge for one session row, or null for "nothing worth saying".
 *
 * The chat being viewed is excluded from its own digest BY DESIGN, so it gets its
 * own neutral badge rather than the "aged out of the window" warning — the reason
 * differs, and under `off` the warning would be actively misleading.
 *
 * The "not in digest" warning only means something under `newest`, where it
 * separates the rows inside the window from the rows outside it. Under `off`
 * nothing is injected, so every row would carry it and it would distinguish
 * nothing — the panel hint says it once instead. Under `relevant` the backend
 * sends no `in_digest` at all (the set is re-chosen per turn), and an absent flag
 * must never render as "excluded".
 */
export function sessionBadge(
  row: Pick<MemorySessionDigest, "in_digest" | "is_active_session">,
  policy: MemoryDigestPolicy,
): SessionBadge | null {
  if (row.is_active_session) {
    return {
      tone: "neutral",
      label: "this chat",
      title:
        "This chat. A session's own summary is never listed as a prior session in its own thread — the agent has the conversation itself.",
    };
  }
  if (policy === "newest" && row.in_digest === false) {
    return { tone: "warning", label: "not in digest", title: NOT_IN_DIGEST_TITLE };
  }
  return null;
}
