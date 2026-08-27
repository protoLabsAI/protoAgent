// ↑-to-edit a queued steer (#2837 follow-up).
//
// The composer's placeholder promises "Press ↑ to edit queued message", but ↑ only ever
// recalled a COPY of the text from the shared input-history ring (#1496): the steer itself
// stayed queued, so re-sending the edit delivered BOTH — the original the operator wanted
// to revise and the revision — and there was no way to take the queued one back short of
// hunting for the ✕ on its bubble. ↑ now PULLS the queued message out of the turn (the same
// server-side dequeue the ✕ performs) and lands its text in the composer, where sending
// queues it afresh.
//
// Which press does what is decided here, purely, so the branch is testable without a DOM:
// an EMPTY composer with something queued pulls the NEWEST steer (LIFO — the same order
// Escape peels them in, see escapeStop.ts); every other press falls through to ordinary
// history nav, including the press right after a pull, so the recalled text is never
// silently eaten by the ring.

export type ComposerUpAction =
  | { kind: "edit-queued"; steerId: string; text: string }
  /** Not a queued-message recall — the caller's ↑/↓ input-history nav applies. */
  | { kind: "history" };

/** Pure decision for one ↑ press in the composer. */
export function resolveComposerUp(
  draft: string,
  steerQueue: readonly { id: string; text: string }[],
): ComposerUpAction {
  // Typing in progress owns ↑ (readline history nav) — a pull would clobber it.
  if (draft.trim()) return { kind: "history" };
  const newest = steerQueue[steerQueue.length - 1];
  if (!newest) return { kind: "history" };
  return { kind: "edit-queued", steerId: newest.id, text: newest.text };
}
