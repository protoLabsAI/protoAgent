// "Toggle latest tool block" keybinding action (ADR 0063, #1526). The tool-call disclosure
// state lives INSIDE the DS `ToolCard` (an uncontrolled `useState`, flipped by clicking its
// `.pl-toolcard__head` toggle) — there's no controlled `open` prop to drive from a store — so
// this walks the rendered chat DOM and clicks the disclosure, exactly as the user would. That
// keeps the streaming spotlight/summary render paths (ToolCalls.tsx / WorkBlock.tsx) untouched
// and works uniformly across every one of them (WorkBlock fold, spotlight, settled cards, the
// summary chip).
//
// Behavior: the first press expands the LATEST top-level tool block; pressing again collapses
// it and moves the walk cursor UPWARD, so subsequent presses expand each older block newest →
// oldest (see `resolveToggle`). A new turn (or a changed block set) resets the walk to the
// latest block. No tool blocks ⇒ no-op.

export type TogglePlan = {
  /** Index (in DOM top→bottom order) of the block to toggle. */
  index: number;
  /** true = expand it, false = collapse it. */
  expand: boolean;
  /** Where the walk cursor lands for the next press. */
  nextCursor: number;
};

/**
 * Pure "toggle latest, then walk upward" decision. `expanded[i]` is each top-level tool
 * block's disclosure state in DOM order (index 0 = topmost/oldest, last = latest/newest).
 * `cursor` is the block the walk currently targets, or null / out-of-range = idle (start at
 * the latest). Returns which block to toggle, whether to expand or collapse it, and the next
 * cursor — or null when there are no blocks (no-op).
 *
 * Sequence for a fresh (idle) walk over a collapsed latest block:
 *   press → expand latest (cursor stays)          [latest open]
 *   press → collapse latest, cursor moves up       [all closed, cursor = prev]
 *   press → expand prev (the "walk upward" step)    … and so on, newest → oldest
 * Collapsing the topmost block sends the cursor out of range, so the next press wraps back to
 * the latest rather than getting stuck.
 */
export function resolveToggle(expanded: boolean[], cursor: number | null): TogglePlan | null {
  const n = expanded.length;
  if (n === 0) return null;
  let idx = cursor;
  if (idx == null || idx < 0 || idx >= n) idx = n - 1; // idle → latest
  if (!expanded[idx]) return { index: idx, expand: true, nextCursor: idx };
  // Already open → collapse it, then walk upward to the previous block for the next press.
  return { index: idx, expand: false, nextCursor: idx - 1 };
}

// The walk cursor + the block set it's keyed to. When the target message or its block count
// changes (a new turn, or tools streaming in), the key changes and the cursor resets to the
// latest — "when idle, target the most recent tool block in the last message".
let walkCursor: number | null = null;
let walkKey = "";

// Stable per-message-node id so two different turns with the same tool-block COUNT still reset
// the walk (React reuses the DOM node within a turn — same id — but mints a new one per turn).
const messageKeys = new WeakMap<Element, number>();
let messageKeySeq = 0;
function keyFor(el: Element): number {
  let k = messageKeys.get(el);
  if (k == null) {
    k = ++messageKeySeq;
    messageKeys.set(el, k);
  }
  return k;
}

/** The visible chat surface — the active session slot, else any chat-scoped root. */
function chatRoot(): Element | null {
  return (
    document.querySelector(".chat-session-slot:not([hidden])") ??
    document.querySelector('[data-kb-scope~="chat"]')
  );
}

/** Top-level TOOL-block disclosure toggles in a message, in DOM (top→bottom) order. Excludes
 *  disabled (body-less) cards, reasoning cards (which render a DS ToolCard but aren't tool
 *  calls — #1526 is "toggle the tool-call block"), and any card nested inside another block's
 *  body/children/summary — a subagent child, a WorkBlock timeline card, or a folded summary
 *  member — so the walk is over top-level tool blocks only. */
export function topLevelToggles(message: Element): HTMLButtonElement[] {
  const heads = message.querySelectorAll<HTMLButtonElement>(
    ".pl-toolcard__head, .pl-toolcard-summary__head",
  );
  const out: HTMLButtonElement[] = [];
  heads.forEach((head) => {
    if (head.disabled) return; // a card with no body can't toggle
    if (head.getAttribute("aria-expanded") == null) return;
    if (head.closest(".reasoning-card")) return; // a reasoning card, not a tool call
    if (head.closest(".pl-toolcard__body, .pl-toolcard__children, .pl-toolcard-summary__body")) {
      return; // nested under another block — not a top-level block
    }
    out.push(head);
  });
  return out;
}

/** The last message that has any top-level tool block, with its toggles (newest last). */
function latestToolBlocks(): { message: Element; toggles: HTMLButtonElement[] } | null {
  const root = chatRoot();
  if (!root) return null;
  const messages = root.querySelectorAll(".pl-message");
  for (let i = messages.length - 1; i >= 0; i--) {
    const toggles = topLevelToggles(messages[i]);
    if (toggles.length) return { message: messages[i], toggles };
  }
  return null;
}

/** Keybinding action (ADR 0063): toggle the latest tool-call block in the current agent
 *  output; repeated presses collapse then walk upward through the block stack. No-op when the
 *  last message has no tool blocks. */
export function toggleLatestToolBlock(): void {
  const found = latestToolBlocks();
  if (!found) return;
  const { message, toggles } = found;
  const key = `${keyFor(message)}:${toggles.length}`;
  if (key !== walkKey) {
    walkKey = key;
    walkCursor = null; // new turn / changed block set → restart the walk at the latest
  }
  const expanded = toggles.map((t) => t.getAttribute("aria-expanded") === "true");
  const plan = resolveToggle(expanded, walkCursor);
  if (!plan) return;
  walkCursor = plan.nextCursor;
  toggles[plan.index].click(); // flip the DS ToolCard's internal disclosure
}

/** Test-only: clear the walk cursor between cases. */
export function __resetToolCollapseWalk(): void {
  walkCursor = null;
  walkKey = "";
}
