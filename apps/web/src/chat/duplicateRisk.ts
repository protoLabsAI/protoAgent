// Known-duplicate composer state for a consumed ↑-recall (#3413).
//
// ↑ on an empty composer pulls a queued steer back out of the running turn to edit it
// (queuedRecall.ts / editQueuedSteer). The server dequeue answers `removed:false` when the
// agent had already read the steer — it's too late to pull it, it already shaped the reply.
// The old behavior fired a transient toast; the recalled text stayed in the composer, so an
// unchanged re-send silently delivered a SECOND copy of a message the turn already saw.
//
// Rather than silently clear or withhold the operator's in-hand edit, we pin an explicit,
// durable "duplicate risk" marker to the EXACT recalled text (+ the session it was recalled
// in) and render a persistent inline warning with deliberate clear / send-anyway actions.
// The marker's whole lifecycle lives here, purely, so the race/clear rules are testable
// without a DOM:
//   * it applies ONLY while the draft is still that exact recalled text, in that session —
//     any edit turns it into a deliberate follow-up (no longer a known duplicate), a clear
//     empties it, and it can never leak onto another session's draft;
//   * a subsequent successful `removed` recall, a deliberate send, or an explicit clear
//     drops it outright.

export type DuplicateRisk = {
  /** The session the consumed recall happened in. The marker never applies to another. */
  sessionId: string;
  /** The exact recalled text. The warning shows only while the draft is still this string. */
  text: string;
};

/** The inline duplicate-risk warning is showing iff a marker is pinned to THIS session and
 *  the draft is still the untouched recalled text. Editing it (a deliberate follow-up),
 *  clearing it, or evaluating it against a different session all read as "not a known
 *  duplicate", so the warning is gone without the state having to be torn down first. */
export function isDuplicateRiskActive(
  risk: DuplicateRisk | null,
  sessionId: string | null | undefined,
  draft: string,
): boolean {
  return !!risk && risk.sessionId === sessionId && draft === risk.text;
}

/** Events that move the marker. `draft` covers both the operator editing the recalled text
 *  into a follow-up and clearing it to empty; `sent` / `clear` / `recall-removed` drop it. */
export type DuplicateRiskEvent =
  /** ↑ recalled a steer the agent had already read (`removed:false`). */
  | { type: "recall-consumed"; sessionId: string; text: string }
  /** ↑ cleanly pulled a steer back out of the turn (`removed:true`) — ordinary recall. */
  | { type: "recall-removed" }
  /** The operator deliberately sent (or queued) the draft. */
  | { type: "sent" }
  /** The operator chose "Clear" on the warning. */
  | { type: "clear" }
  /** The draft or the active session changed. */
  | { type: "draft"; sessionId: string | null | undefined; draft: string };

/** Reduce the marker across one event. Returns the SAME reference when nothing changed so a
 *  functional `setState` can dispatch this on every keystroke without churning renders. */
export function nextDuplicateRisk(
  risk: DuplicateRisk | null,
  event: DuplicateRiskEvent,
): DuplicateRisk | null {
  switch (event.type) {
    case "recall-consumed":
      return { sessionId: event.sessionId, text: event.text };
    case "recall-removed":
    case "sent":
    case "clear":
      return null;
    case "draft":
      // Keep the marker only while it is still an active known duplicate; any divergence
      // (edited, cleared, or a different session) drops it.
      return isDuplicateRiskActive(risk, event.sessionId, event.draft) ? risk : null;
  }
}
