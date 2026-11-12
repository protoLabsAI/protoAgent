/** People fit on the chat status rail (work consolidation v2 S6b, the v7 people behavior).
 *
 *  The rail keeps the group chips and the people on ONE row — nothing wraps and nothing
 *  scrolls sideways. Whoever doesn't fit collapses behind a `+K` chip that opens the picker
 *  ("In this chat · N"). Everything here is pure (order, caps, fit arithmetic) so the row
 *  behavior is testable without a layout engine; the component feeds it real measurements
 *  from a hidden mirror of the row (see ChatStatusRail) via ResizeObserver.
 */
import { leadingRun } from "./mentionRun";

/** The cast's spelling of a run-mentioned name. The run is typed text, so the match is
 *  case-insensitive; the CAST spelling is what renders. Undefined when the run mentions
 *  someone who isn't in the cast. */
function castSpelling(cast: string[], name: string): string | undefined {
  const lower = name.toLowerCase();
  return cast.find((c) => c.toLowerCase() === lower);
}

/** Display order: draft-addressed names first (in the order the leading run addresses
 *  them), then everyone else in the original cast order. A name picked in the picker
 *  joins the draft's run, so it moves into the addressed front group — that is how a
 *  newly picked name takes a visible slot. Run mentions that name nobody in the cast
 *  don't reorder anything. */
export function orderPeople(cast: string[], draft: string): string[] {
  const addressed: string[] = [];
  for (const name of leadingRun(draft).names) {
    const match = castSpelling(cast, name);
    if (match && !addressed.includes(match)) addressed.push(match);
  }
  return [...addressed, ...cast.filter((name) => !addressed.includes(name))];
}

/** How many names to show before the row has ever been measured (first paint, and any
 *  environment without layout). Deliberately conservative so the unmeasured row cannot
 *  overflow: with status chips sharing the row the people get less room (desktop 3,
 *  phone 1); with no statuses the people own the row (desktop 5, phone 3). The first
 *  ResizeObserver delivery replaces the cap with the measured fit. */
export function fallbackCap(mobile: boolean, hasStatusChips: boolean): number {
  if (mobile) return hasStatusChips ? 1 : 3;
  return hasStatusChips ? 3 : 5;
}

/** One measured chip in the hidden mirror row: its left edge and width, both relative to
 *  the row that also contains the leading people icon — so a chip's `left` already
 *  carries the icon, the gaps, and every chip before it. */
export type ChipBox = { left: number; width: number };

/** How many of the ordered chips fit in `available` px.
 *
 *  `boxes` is every chip laid out in display order (the hidden mirror row), `moreWidth`
 *  the measured width of the widest `+K` chip. If the last chip's right edge fits, all
 *  of them do — no `+K`. Otherwise show the longest prefix that leaves room for `+K`
 *  IN the first hidden chip's slot: with chips 0..k-1 visible, `+K` sits exactly where
 *  chip k would (same gap arithmetic), so the test is `boxes[k].left + moreWidth`.
 *  A row too narrow for even one name shows `+K` alone (floor 0, never overflow). */
export function fitCount(boxes: ChipBox[], moreWidth: number, available: number): number {
  const n = boxes.length;
  if (!n) return 0;
  const last = boxes[n - 1];
  if (last.left + last.width <= available) return n;
  for (let k = n - 1; k >= 1; k--) {
    if (boxes[k].left + moreWidth <= available) return k;
  }
  return 0;
}

/** The picker's dynamic recipients line: "Your message goes to @opus, @Vera. Choose a
 *  name again to take it out." — the draft's leading run IS where the message routes,
 *  so the line lists the run (in run order), rendered with the cast's spelling when a
 *  cast member matches. Null when the run addresses nobody: no recipients, no line. */
export function recipientLine(draft: string, cast: string[]): string | null {
  const names = leadingRun(draft).names.map((name) => castSpelling(cast, name) ?? name);
  if (!names.length) return null;
  return `Your message goes to ${names.map((n) => `@${n}`).join(", ")}. Choose a name again to take it out.`;
}

/** The complete spoken label for the `+K` chip — the visible text is just "+K". */
export function spokenMoreLabel(hiddenCount: number): string {
  return `${hiddenCount} more ${hiddenCount === 1 ? "person" : "people"} in this chat. Show everyone`;
}
