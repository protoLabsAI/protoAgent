// A server turn's canonical text belongs to the TURN, not to a message.
//
// One assistant turn usually renders as one bubble, but the console deliberately
// splits that bubble to place something at the point in the stream where it
// happened: a consumed mid-turn steer (#3150) or a delegation exchange (#3042).
// `insertConversationBubbles` freezes what the turn had said so far as its own
// bubble and leaves an EMPTY continuation under the original id, so streaming
// carries on below the inserted row.
//
// Meanwhile every authoritative text frame — the A2A terminal artifact-update, a
// Task snapshot replay, a `tasks/get` reconcile — carries the WHOLE turn's text
// with `append` false (#1717: the terminal frame is the full canonical answer,
// pre-tool narration included). Applied to a single message that assumption is
// right; applied to a split turn it re-lands text an earlier bubble of the SAME
// turn already shows, and the answer renders twice.
//
// So canonical text is DISTRIBUTED across a turn's bubbles here, exactly once,
// instead of being assigned to whichever bubble the stream happens to be holding.
// Pure + unit-tested (turnText.test.ts); the wire-level guard is the interjection
// case in e2e/double-render.spec.ts. (#3387 — distinct from #1938, whose store guard
// dedupes by ID and so cannot see a split that mints two on purpose.)

import type { ChatMessage, ChatPart } from "../lib/types";
import { replaceText } from "./parts";
import { isEmptyPlaceholder } from "./roomBubble";

/** Nothing of this turn's own to show: no answer text, no ordered parts, no tool
 *  cards, no reasoning, no inline component. A split turn's continuation looks
 *  like this until the agent says something after the inserted row. */
function carriesNothing(message: ChatMessage): boolean {
  return isEmptyPlaceholder(message) && !message.components?.length;
}

function withoutText(parts: ChatPart[] | undefined): ChatPart[] {
  return (parts ?? []).filter((part) => part.kind !== "text");
}

/** The text this bubble currently RENDERS: its ordered parts when it has them, the
 *  flat `content` otherwise — the same choice ChatMessageView makes. The two can
 *  disagree: a settled tool/component reply whose prose only ever reached `content`
 *  shows its cards and no answer (#3340). It is the RENDERED text that decides
 *  whether canonical text still has to be landed, and what a later bubble of the
 *  same turn must not repeat. */
function shownText(message: ChatMessage): string {
  if (!message.parts?.length) return message.content;
  return message.parts
    .filter((part) => part.kind === "text")
    .map((part) => part.text)
    .join("");
}

/** Land canonical text on ONE bubble.
 *
 *  Keeps whatever status the bubble already carries: a canonical replace also
 *  arrives for turns something has already settled (a watchdog finalize, a Stop, a
 *  post-stream reconcile, hydration repair), and stamping "streaming" there would
 *  resurrect a finished turn's spinner — the guard the reveal queue also makes. */
function landText(message: ChatMessage, text: string): ChatMessage {
  return {
    ...message,
    content: text,
    parts: replaceText(message.parts, text, shownText(message)),
    status: message.status ?? "streaming",
  };
}

/** Indexes of the assistant bubbles rendering ONE server turn, in transcript order.
 *
 *  The link is explicit, not inferred: an inline split stamps `splitOf` on the half
 *  it freezes, naming the continuation that keeps streaming. So a turn is its
 *  continuation plus every bubble frozen out of it — however many steers or
 *  delegations cut it, and whatever rows were inserted between them. Passing the id
 *  of a frozen half resolves to the same turn.
 *
 *  Deliberately NOT "every assistant message sharing this taskId": a task id can
 *  outlive one rendered turn (a HITL resume continues the same task), and that
 *  grouping would silently reach across a boundary it has no business crossing.
 *
 *  Returns [] when the anchor is gone (a cleared/rewound transcript), and a single
 *  index for the ordinary un-split turn — the overwhelmingly common case. */
export function turnBubbleIndexes(messages: ChatMessage[], assistantId: string): number[] {
  // The continuation may already be GONE — `settleTurnBubbles` folds one away when the
  // canonical text left it empty — while callers still hold its id and have more to
  // reconcile (the post-stream durable-task heal, hydration repair). A surviving half
  // still names it in `splitOf`, so the turn is findable from either end; without this
  // those callers silently no-op on exactly the turns that were split.
  const anchor =
    messages.find((message) => message.id === assistantId) ??
    messages.find((message) => message.splitOf === assistantId);
  if (!anchor) return [];
  const liveId = anchor.splitOf ?? assistantId;
  const indexes: number[] = [];
  messages.forEach((message, index) => {
    if (message.id === liveId || (message.splitOf && message.splitOf === liveId)) indexes.push(index);
  });
  return indexes;
}

/** Where `canonical` continues past the text `prefix` already renders, or -1 when
 *  `prefix` is not a prefix of it.
 *
 *  Compared whitespace-INSENSITIVELY on purpose. The two strings are accumulated by
 *  different sides: the server injects a blank line between pre- and post-tool
 *  narration (#3210) that the client's own delta accumulation never had, and
 *  `appendText` drops a run's leading whitespace. A byte-exact test would call those
 *  healthy turns "diverged" and rebuild them, throwing away the interleaving for
 *  nothing. Non-whitespace characters must still match exactly, in order. */
export function canonicalRemainderIndex(canonical: string, prefix: string): number {
  const space = /\s/;
  let at = 0;
  let want = 0;
  while (want < prefix.length) {
    if (space.test(prefix[want])) {
      want += 1;
      continue;
    }
    while (at < canonical.length && space.test(canonical[at])) at += 1;
    if (at >= canonical.length || canonical[at] !== prefix[want]) return -1;
    at += 1;
    want += 1;
  }
  return at;
}

/** Land a turn's canonical full-turn text across its bubbles, exactly once.
 *
 *  Un-split turn: identical to the per-message `applyText(m, text, false)` it
 *  replaces — the overwhelmingly common path, unchanged.
 *
 *  Split turn: the earlier bubbles keep the text they already show and the trailing
 *  bubble takes the REMAINDER, so the inserted steer/delegation stays at the
 *  boundary the agent actually consumed it. When the earlier bubbles no longer
 *  describe a prefix of the canonical answer their placement can't be trusted, so
 *  fall back the same way `replaceText` does within a single bubble: strip their
 *  text (their tool cards and reasoning stay) and land the whole answer on the
 *  trailing bubble. Interleaving degrades; "exactly once" holds. */
export function applyCanonicalTurnText(
  messages: ChatMessage[],
  assistantId: string,
  canonical: string,
): ChatMessage[] {
  const indexes = turnBubbleIndexes(messages, assistantId);
  if (!indexes.length) return messages;
  const tail = indexes[indexes.length - 1];
  if (indexes.length === 1) {
    return messages.map((message, index) => (index === tail ? landText(message, canonical) : message));
  }
  const lead = indexes.slice(0, -1);
  const shown = lead.map((index) => shownText(messages[index])).join("");
  const at = canonicalRemainderIndex(canonical, shown);
  if (at >= 0) {
    const remainder = canonical.slice(at).replace(/^\s+/, "");
    return messages.map((message, index) => (index === tail ? landText(message, remainder) : message));
  }
  // Stripping a PROSE-ONLY earlier half leaves a bubble with nothing in it, and the
  // transcript draws a row for every message — so drop the ones that end up empty,
  // the same way a spent continuation is folded. Halves that still carry tool cards
  // or reasoning stay: they are the record of what the turn did.
  const diverged = new Set(lead);
  const stripped = messages.map((message, index) => {
    if (index === tail) return landText(message, canonical);
    if (!diverged.has(index)) return message;
    return { ...message, content: "", parts: withoutText(message.parts) };
  });
  return stripped.filter((message, index) => !diverged.has(index) || !carriesNothing(message));
}

/** Collapse a split turn back to its anchor before an authoritative Task snapshot
 *  is replayed into it.
 *
 *  A snapshot is authoritative for the WHOLE turn, and a snapshot's artifacts
 *  flatten every text frame into one accumulation — which is exactly why the frame
 *  dispatcher refuses to replay steer markers from task history (their position
 *  relative to that text cannot be reconstructed honestly). Keeping the earlier
 *  bubbles would leave their prose beside a replay that also contains it, so the
 *  turn folds back into a single bubble and the replay refills it. The inserted
 *  user/participant rows stay where they are; the answer simply lands below them —
 *  the same conservative placement the turn-end fallback uses. */
export function resetTurnForSnapshot(messages: ChatMessage[], assistantId: string): ChatMessage[] {
  const indexes = turnBubbleIndexes(messages, assistantId);
  if (indexes.length < 2) return messages;
  const dropped = new Set(indexes.slice(0, -1));
  return messages.filter((_, index) => !dropped.has(index));
}

/** Fold away a split turn's trailing bubble when the canonical text left it empty.
 *
 *  The agent can say everything it has to say BEFORE the steer it then consumes; the
 *  continuation opened for text that never came, and settling it would draw a blank
 *  row under the answer. Its turn footer (spend, context fill) belongs to the bubble
 *  that actually shows the answer, so it moves there. Called at settle time only —
 *  mid-turn the continuation is legitimately empty and still expects text. */
export function settleTurnBubbles(messages: ChatMessage[], assistantId: string): ChatMessage[] {
  const indexes = turnBubbleIndexes(messages, assistantId);
  if (indexes.length < 2) return messages;
  const tail = indexes[indexes.length - 1];
  if (!carriesNothing(messages[tail])) return messages;
  const lead = indexes[indexes.length - 2];
  const footer = messages[tail];
  return messages
    .map((message, index) =>
      index === lead
        ? {
            ...message,
            // The continuation is where the turn's OUTCOME was stamped: a failed turn
            // settles it "error", and folding it away without carrying that would
            // render the failure as a clean answer, with no Dismiss. The half being
            // folded into was frozen "done" at split time and knows nothing of it.
            status: footer.status === "error" ? "error" : message.status,
            usage: message.usage ?? footer.usage,
            contextWindow: message.contextWindow ?? footer.contextWindow,
          }
        : message,
    )
    .filter((_, index) => index !== tail);
}

/** One-time repair of transcripts persisted BEFORE this fix.
 *
 *  A split turn whose continuation was handed the whole canonical answer shows the
 *  earlier half's prose a second time, and nothing heals it: the duplicate is in
 *  `localStorage`, `dedupeMessages` only collapses colliding IDS (the split mints two
 *  on purpose), and boot hydration skips a session that already has messages.
 *
 *  This groups by `taskId`, which `turnBubbleIndexes` deliberately refuses to do — so
 *  be clear about why the two differ rather than reading one as a violation of the
 *  other. They answer different questions:
 *
 *  - `turnBubbleIndexes` decides where to WRITE new canonical text. Reaching across a
 *    turn boundary there would overwrite an unrelated turn's good text, so it needs
 *    exact turn identity and takes it from the explicit `splitOf` link.
 *  - this decides whether text ALREADY RENDERED is being shown twice. Its condition —
 *    a later bubble of the same task whose rendered text begins with the whole of an
 *    earlier one's — is itself the proof: that prose is on screen twice, and the
 *    second copy should go whatever seam produced it. Pre-fix data carries no
 *    `splitOf` to group by, so the task is the only handle it has.
 *
 *  Two further guards keep it off anything it shouldn't touch. A group containing a
 *  `splitOf` bubble was written by the fixed code and is correct by construction, so
 *  it is skipped — which also makes this migration self-limiting, inert once history
 *  has turned over. And the prefix must be whole and non-empty; a turn that merely
 *  opens the way an earlier one did is left alone.
 *
 *  (Console turns get their own task id today — 27 unique across the 27-turn session
 *  that surfaced #3387, HITL park included — so "several rendered turns in one task"
 *  is not a shape this can currently meet. The guards hold if that ever changes.)
 *
 *  Applied at load; message statuses untouched. */
export function repairDuplicatedTurnText(messages: ChatMessage[]): ChatMessage[] {
  const byTask = new Map<string, number[]>();
  for (const [index, message] of messages.entries()) {
    if (message.role !== "assistant" || !message.taskId || message.author) continue;
    const bubbles = byTask.get(message.taskId) ?? [];
    bubbles.push(index);
    byTask.set(message.taskId, bubbles);
  }
  const repaired = new Map<number, ChatMessage>();
  const dropped = new Set<number>();
  for (const bubbles of byTask.values()) {
    if (bubbles.length < 2) continue;
    if (bubbles.some((index) => messages[index].splitOf)) continue; // written by the fixed path
    const tail = bubbles[bubbles.length - 1];
    const shown = bubbles
      .slice(0, -1)
      .map((index) => shownText(messages[index]))
      .join("");
    if (!shown.trim()) continue;
    const message = messages[tail];
    const duplicated = shownText(message);
    const at = canonicalRemainderIndex(duplicated, shown);
    if (at <= 0) continue;
    const remainder = duplicated.slice(at).replace(/^\s+/, "");
    const next: ChatMessage = {
      ...message,
      content: remainder,
      parts: remainder
        ? withoutText(message.parts).concat({ kind: "text", text: remainder })
        : withoutText(message.parts),
    };
    if (carriesNothing(next)) dropped.add(tail);
    else repaired.set(tail, next);
  }
  if (!repaired.size && !dropped.size) return messages;
  return messages
    .map((message, index) => repaired.get(index) ?? message)
    .filter((_, index) => !dropped.has(index));
}
