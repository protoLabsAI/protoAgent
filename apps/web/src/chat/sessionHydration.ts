// Durable chat recovery (#2888, ADR 0104). The server returns the raw A2A
// task wire; user text becomes the prompt bubble and the assistant side runs
// through the same snapshot dispatcher + reducers as live/reattached turns.

import {
  api,
  replayDurableChatTurn,
  supersededByFromStatus,
  textFromParts,
  type DurableChatSession,
  type DurableChatTurn,
} from "../lib/api";
import type { ChatMessage } from "../lib/types";
import {
  chatStore,
  dedupeMessages,
  DEFAULT_SESSION_TITLE,
  needsDurableHydration,
  type ChatSession,
  type HydrationEligibility,
  type SessionReconcile,
} from "./chat-store";
import { onTopic } from "../lib/events";
import { applyDelegateProgressTo } from "./delegateProgress";
import { attachmentDisplay, displayWithoutThumbnailNames, validThumbnails } from "./imageThumbnails";
import { rendersText, replaceText, textRuns } from "./parts";
import { isEmptyPlaceholder, replyClaimsAnswer, roomReplyBubble } from "./roomBubble";
import { localTurnInFlight, reconcileSessionStatus } from "./sessionLiveness";
import { isTaskCanceled, isTaskFailed, isTaskPaused, isTaskTerminal } from "./taskState";
import { applyComponent, applyDecisionIdentity, applyReasoning, applyText, applyToolEvent, applyUsage, pauseBubble, stopBubble } from "./turnReducers";

// A date-time with no zone designator: `2026-10-09T23:11:04` / `2026-10-09 23:11:04.123`.
const ZONELESS_DATE_TIME = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/;

/** Epoch ms for a server timestamp, or NaN. The task store records `last_updated` in UTC;
 *  older servers sent it with no zone, which `Date.parse` reads as LOCAL time — an operator
 *  at UTC-7 saw a chat stamped seven hours early (#4145). A zoneless value is UTC. */
export function parseServerInstant(value: string | null | undefined): number {
  if (!value) return NaN;
  const text = value.trim();
  return Date.parse(ZONELESS_DATE_TIME.test(text) ? `${text.replace(" ", "T")}Z` : text);
}

function timestamp(value: string | null): number {
  const parsed = parseServerInstant(value);
  return Number.isFinite(parsed) ? parsed : Date.now();
}

/** The instant the store recorded, or `undefined` when it has none. A MESSAGE's `createdAt`
 *  is shown to the operator as when it was sent, so it is never the hydration-time "now"
 *  `timestamp` falls back to for session ordering. */
function recordedAt(value: string | null): number | undefined {
  const parsed = parseServerInstant(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

type DurableMessage = NonNullable<DurableChatTurn["history"]>[number];

/** A durable user frame the OPERATOR sent — the console's own send. A server-fired
 *  turn (scheduler / watch / background-resume) also starts with a user-role message,
 *  but it carries machine text and the `origin` that fired it. */
function isOperatorMessage(message: DurableMessage): boolean {
  const role = (message.role ?? "").toLowerCase();
  if (role !== "user" && !role.includes("role_user")) return false;
  const origin = message.metadata?.origin;
  return !(typeof origin === "string" && origin);
}

/** The operator bubble this turn showed live, or "" for none. The server already stores
 *  each turn's opening message that way (ADR 0104): the bubble text for a `display`
 *  send, no text for a `hidden` one (an approval or dismissal resume, a regenerate, a
 *  goal kickoff), nothing for a server-fired turn. The same rules apply here to any row,
 *  whoever wrote it. A turn stored before the server kept prompts has no user message at
 *  all and rebuilds as its answer alone. */
function operatorPrompt(turn: DurableChatTurn): string {
  const user = (turn.history ?? []).find(isOperatorMessage);
  if (!user || user.metadata?.hidden === true) return "";
  const display = user.metadata?.display;
  return typeof display === "string" ? display : textFromParts(user.parts);
}

/** The bounded image thumbnails the operator's send carried, kept on the durable turn
 *  (ADR 0104, bd-62xi) and restored onto the rebuilt user bubble so a chat opened in
 *  another browser shows the same inline previews. `[]` when the turn stored none (older
 *  chats, a preview that failed, a hidden send): the `display` text names every image, so
 *  that "Attached: <name>" fallback stands. `validThumbnails` re-checks the bound + the
 *  data-URL shape here too, so a hand-edited or oversized entry never reaches render. */
function operatorThumbnails(turn: DurableChatTurn): ChatMessage["attachments"] {
  const user = (turn.history ?? []).find(isOperatorMessage);
  if (!user || user.metadata?.hidden === true) return [];
  return validThumbnails(user.metadata?.thumbnails);
}

/** Incognito is per operator message and therefore must be recovered from the
 * newest durable OPERATOR frame — hidden sends included (the console stamps every
 * send), server-fired ones not (they never carry the flag). Defaulting a recovered
 * private tab to ordinary would let its next send participate in memory without the
 * operator opting in. */
function durableIncognito(turns: DurableChatTurn[]): boolean {
  for (const turn of [...turns].reverse()) {
    const user = [...(turn.history ?? [])].reverse().find(isOperatorMessage);
    if (user) return user.metadata?.incognito === true;
  }
  return false;
}

function titleFromPrompt(prompt: string): string {
  const text = prompt.trim();
  if (!text) return DEFAULT_SESSION_TITLE;
  return text.length > 52 ? `${text.slice(0, 49)}...` : text;
}

/** Pure conversion of one task into the prompt, the interjections the agent read
 *  mid-turn, and the answer. */
export function messagesFromDurableTurn(turn: DurableChatTurn): ChatMessage[] {
  // A durable turn records only when it last changed — for a finished turn, when its REPLY
  // landed. That is the final answer's sent time; the prompt and any interjection were sent
  // earlier, at a time the store never kept, so they carry no `createdAt` rather than a
  // borrowed one (the sent-time footer shows nothing for them instead of the wrong time).
  const repliedAt = recordedAt(turn.last_updated);
  const prompt = operatorPrompt(turn);
  // Restored thumbnails are SHOWN inline, so strip their names from the bubble text — the
  // rebuilt bubble then reads exactly as the live one did (typed text + inline previews)
  // instead of naming an image it also shows. With no thumbnails this is a no-op and the
  // "Attached: <name>" text stands as the fallback.
  const thumbnails = operatorThumbnails(turn);
  const content = thumbnails?.length
    ? displayWithoutThumbnailNames(prompt, thumbnails.map((t) => t.name))
    : prompt;
  const messages: ChatMessage[] = prompt
    ? [
        {
          id: `durable-${turn.task_id}-user`,
          role: "user",
          content,
          status: "done",
          ...(thumbnails?.length ? { attachments: thumbnails } : {}),
        },
      ]
    : [];
  const anchorId = `durable-${turn.task_id}-assistant`;
  const fresh = (): ChatMessage => ({
    id: anchorId,
    role: "assistant",
    content: "",
    status: "streaming",
    taskId: turn.task_id,
  });
  let assistant = fresh();
  // What the turn had already shown when the agent read an interjection, plus the
  // interjection itself. The turn's trailing bubble keeps the anchor id, so the halves
  // frozen ahead of it name it in `splitOf` — one turn, several bubbles (turnText.ts).
  const settled: ChatMessage[] = [];
  const terminal = isTaskTerminal(turn.state);
  // An `@`-addressed turn's answer is its participants' bubbles; its stored answer text
  // only restates them for clients that draw no bubbles (#3449), so it must not land too.
  let answeredByParticipants = false;
  // A decision handle belongs to its pause bubble even when no text/tool frame was stored.
  const freeze = () => {
    if (!isEmptyPlaceholder(assistant) || assistant.decisionId) {
      settled.push({
        ...assistant,
        id: `${anchorId}-${settled.length}`,
        status: "done",
        splitOf: anchorId,
        toolCalls: assistant.toolCalls?.map((call) =>
          call.status === "running" ? { ...call, status: "done" as const } : call,
        ),
      });
      assistant = fresh();
    }
  };
  replayDurableChatTurn(turn, "", {
    onText: (text, append) => {
      if (answeredByParticipants) return;
      assistant = applyText(assistant, text, append);
    },
    onRoomReply: (reply) => {
      // A participant's reply / the lead's ask / the room's note, where it happened — the
      // live stream's insertRoomBubble split, rebuilt: the work before it freezes, the
      // bubble follows, the lead continues in a fresh one.
      const bubble = roomReplyBubble(reply, `${anchorId}-room-${settled.length}`, {
        ...(repliedAt !== undefined ? { createdAt: repliedAt } : {}),
        turnTaskId: turn.task_id,
      });
      if (!bubble) return;
      freeze();
      settled.push(bubble);
      if (replyClaimsAnswer(reply)) answeredByParticipants = true;
    },
    onSteerConsumed: (items) => {
      // The agent read the operator's interjection HERE — between the work above and
      // the work below — so the rebuilt transcript splits there, exactly as the live
      // one does (roomBubble.insertConversationBubbles). The ANSWER lands on the
      // trailing bubble: the durable artifacts flatten every text frame into one
      // accumulation, so how much of the prose preceded the interjection is not
      // recoverable. Ids are the steer's own, so a later live settle is a no-op
      // (steerPlacement.placeConsumedSteers dedupes on them).
      if (!isEmptyPlaceholder(assistant) || assistant.decisionId) {
        settled.push({ ...assistant, id: `${anchorId}-${settled.length}`, status: "done", splitOf: anchorId });
      }
      settled.push(
        ...items.map((item, index) => ({
          id: item.id,
          role: "user" as const,
          content: item.text,
          status: "done" as const,
        })),
      );
      assistant = fresh();
    },
    onContinuationMessage: (message) => {
      // The operator's answer to a paused turn, sent on the SAME task (#3930): the turn
      // splits there, exactly as it did live — the work before the pause, the answer
      // bubble, then the work after. A hidden send (an approval, a dismissal, a settle)
      // continued the bubble without one, so it splits nothing.
      if (!isOperatorMessage(message) || message.metadata?.hidden === true) return;
      const display = message.metadata?.display;
      const text = typeof display === "string" ? display : textFromParts(message.parts);
      if (!text) return;
      if (!isEmptyPlaceholder(assistant) || assistant.decisionId) {
        settled.push({
          ...assistant,
          id: `${anchorId}-${settled.length}`,
          status: "done",
          splitOf: anchorId,
          toolCalls: assistant.toolCalls?.map((call) =>
            call.status === "running" ? { ...call, status: "done" as const } : call,
          ),
        });
      }
      settled.push({ id: `${anchorId}-answer-${settled.length}`, role: "user", content: text, status: "done" });
      assistant = fresh();
    },
    onHitlPayload: (payload) => {
      assistant = applyDecisionIdentity(assistant, payload);
    },
    onReasoning: (delta) => {
      assistant = applyReasoning(assistant, delta);
    },
    onToolCall: (event) => {
      if (event.name !== "show_component") assistant = applyToolEvent(assistant, event);
    },
    onComponent: (spec) => {
      assistant = applyComponent(assistant, spec);
    },
    onDelegateProgress: (evt) => {
      // An `@` mention card's coding delegate, at its final (durably kept) state (#3979).
      assistant = applyDelegateProgressTo(assistant, evt);
    },
    onCost: (usage) => {
      assistant = applyUsage(assistant, usage);
    },
    onContext: (contextWindow) => {
      assistant = { ...assistant, contextWindow };
    },
  });
  if (terminal) {
    assistant = isTaskCanceled(turn.state)
      ? // A STOPPED turn (#4076): a neutral "Stopped" bubble whose unfinished cards read
        // stopped — not an error, and not a ✓ on a call that never returned.
        { ...stopBubble(assistant), ...(repliedAt !== undefined ? { createdAt: repliedAt } : {}) }
      : {
          ...assistant,
          ...(repliedAt !== undefined ? { createdAt: repliedAt } : {}),
          status: isTaskFailed(turn.state) ? "error" : "done",
          toolCalls: assistant.toolCalls?.map((call) =>
            call.status === "running" ? { ...call, status: "done" as const } : call,
          ),
        };
    // ChatMessageView draws a parts-bearing bubble FROM its ordered parts and only
    // falls back to `content` when there are none — so a completed multi-part turn
    // (tool cards/components + reply) whose answer text landed only in flat
    // `content` rehydrates with the cards but no reply: switching agents and back
    // would drop the assistant's prose (#3340). The server ships the joined answer
    // as `turn.text`; when ordered parts exist but do not carry that answer,
    // reconcile it in as the trailing run — the same seam reattach.finalize closes
    // on the live resubscribe path, applied here to durable hydration. A turn that
    // already surfaced the full ordered text is left untouched.
    if (
      !answeredByParticipants &&
      turn.text &&
      assistant.parts?.length &&
      !rendersText(textRuns(assistant.parts), turn.text)
    ) {
      assistant = { ...assistant, content: turn.text, parts: replaceText(assistant.parts, turn.text) };
    }
  } else {
    // Keep the durable partial visible if a cold/failed reattach cannot produce
    // a newer Task snapshot. The reattach handler recognizes this marker and
    // clears snapshot-derived fields immediately before authoritative replay.
    assistant = { ...assistant, durableSnapshotFallback: true };
    // A turn PARKED on the operator renders as waiting from the first paint (#3946) — even
    // in a session whose slot never mounts a reattach. The reattach re-marks it off the
    // live snapshot; the answer that continues the turn clears it.
    if (isTaskPaused(turn.state)) assistant = pauseBubble(assistant);
  }
  // A turn the agent had nothing left to say after — everything it did came before the
  // last interjection — would otherwise settle as a blank row under it (the live path's
  // `settleTurnBubbles` folds the same case away). A non-terminal turn keeps its empty
  // trailing bubble: that is the one a reattach streams into.
  const turnBubbles =
    settled.length && terminal && isEmptyPlaceholder(assistant) && !assistant.decisionId && !assistant.components?.length
      ? settled
      : [...settled, assistant];
  // Stamped on the turn's own bubbles, as the live settle does, so no later producer of
  // the canonical text (reconcile, reattach, a re-hydration) lands it on them either.
  const stamped = answeredByParticipants
    ? turnBubbles.map((m) => (m.author || m.role !== "assistant" ? m : { ...m, answeredByParticipants: true }))
    : turnBubbles;
  return [...messages, ...stamped];
}

/** How the transcript draws a session's durable turns (#3963): `turns` in drawing order,
 *  and the ids of the turns QUEUED behind the live one.
 *
 *  Everything downstream reads the session's LAST assistant bubble as its live turn: boot
 *  mounts the slot off it, the reattach resubscribes to its task, a HITL answer continues
 *  it. So the turn running or waiting on the operator must own the last assistant bubble,
 *  whatever order the rows arrived in:
 *
 *  - An older server ordered rows by when each last CHANGED, and a pause a plain message
 *    moved to a new task left the old task to complete ("Continued in task …") just AFTER
 *    the new one parked: the completion came last, and the parked turn sat mid-transcript.
 *  - A turn queued behind the running one (a scheduled fire, a background nudge, another
 *    client) is created — and marked working — before it waits for the session's lock, so
 *    it is the NEWER row. It has produced nothing and will run after the live turn; it is
 *    drawn after it as its prompt alone, so the running turn keeps the live bubble.
 *
 *  The live turn is the server's `live_task_id`. A server that predates it (the field is
 *  absent) gets the narrowest inference that covers the first quirk: the last row not yet
 *  over, when every row after it is a task whose pause it took over (a completion pointing
 *  elsewhere, supersededByFromStatus) — never an older orphan that merely never ended.
 *
 *  Once the live turn is known, a PARKED row that is not it lost its pause to a newer task
 *  (one pause per context, #3930): it is over, and renders so rather than as a second
 *  "waiting for your input". With nothing known, rows are drawn as they came. */
export function planDurableTurns(
  turns: DurableChatTurn[],
  liveTaskId?: string | null,
): { turns: DurableChatTurn[]; queued: Set<string> } {
  let live: DurableChatTurn | undefined;
  if (liveTaskId !== undefined) {
    live = liveTaskId ? turns.find((turn) => turn.task_id === liveTaskId) : undefined;
  } else {
    let at = turns.length - 1;
    while (at >= 0 && isTaskTerminal(turns[at].state)) at -= 1;
    if (at >= 0 && turns.slice(at + 1).every((turn) => supersededByFromStatus(turn.status))) live = turns[at];
  }
  if (!live && liveTaskId === undefined) return { turns, queued: new Set() };
  // Only a marking server's order is creation order: its WORKING rows after the live one
  // are queued behind it (a parked one there lost its pause, below).
  const liveAt = live ? turns.indexOf(live) : -1;
  const queued = live && liveTaskId !== undefined
    ? turns.filter((turn, index) => index > liveAt && !isTaskTerminal(turn.state) && !isTaskPaused(turn.state))
    : [];
  const settled = turns
    .filter((turn) => turn !== live && !queued.includes(turn))
    .map((turn) => (isTaskPaused(turn.state) ? { ...turn, state: "TASK_STATE_COMPLETED" } : turn));
  return {
    turns: [...settled, ...(live ? [live] : []), ...queued],
    queued: new Set(queued.map((turn) => turn.task_id)),
  };
}

/** The drawing order alone (see planDurableTurns). */
export function orderDurableTurns(turns: DurableChatTurn[], liveTaskId?: string | null): DurableChatTurn[] {
  return planDurableTurns(turns, liveTaskId).turns;
}

/** The transcript messages for a session's durable turns, drawn per planDurableTurns: a
 *  queued turn shows only its prompt (it has said nothing yet). */
export function messagesFromDurableTurns(rows: DurableChatTurn[], liveTaskId?: string | null): ChatMessage[] {
  const { turns, queued } = planDurableTurns(rows, liveTaskId);
  return turns.flatMap((turn) =>
    queued.has(turn.task_id)
      ? messagesFromDurableTurn(turn).filter((message) => message.role === "user")
      : messagesFromDurableTurn(turn),
  );
}

/** Build one fixed-id local session from its durable turns (see orderDurableTurns for the
 *  order they are drawn in, and `liveTaskId`). */
export function sessionFromDurableTurns(
  summary: DurableChatSession,
  rows: DurableChatTurn[],
  liveTaskId?: string | null,
): ChatSession | null {
  const turns = orderDurableTurns(rows, liveTaskId);
  const messages = messagesFromDurableTurns(rows, liveTaskId);
  if (!messages.length) return null;
  const createdAt = timestamp(turns[0]?.last_updated ?? summary.last_updated);
  const updatedAt = timestamp(summary.last_updated);
  // An image-only send shows its thumbnail inline, so its bubble content is empty — title
  // it from the attachment names instead, exactly as the live store's `titleFromMessages`
  // does, so a rebuilt image chat reads "Attached: <name>" rather than "New chat".
  const firstUser = messages.find((message) => message.role === "user");
  const firstPrompt =
    firstUser?.content.trim() || attachmentDisplay("", firstUser?.attachments?.map((a) => a.name) ?? []);
  return {
    id: summary.session_id,
    title: titleFromPrompt(firstPrompt),
    messages,
    createdAt,
    updatedAt,
    ...(durableIncognito(turns) ? { incognito: true } : {}),
  };
}

/** A bubble of the session's OWN turns — not a participant's (`author`), and tied to a task. */
function isOwnTurnBubble(message: ChatMessage): boolean {
  return message.role === "assistant" && !message.author && Boolean(message.taskId);
}

/** The durable task ids this local transcript already holds a turn for. */
export function localTurnIds(messages: ChatMessage[]): Set<string> {
  return new Set(messages.filter(isOwnTurnBubble).map((message) => message.taskId as string));
}

/** Has the server recorded turns this NON-EMPTY local copy never saw? Another browser,
 *  window or client ran a turn in the session after this cache last wrote it — the local
 *  transcript must not win forever over a server that has more (ADR 0104). The index's
 *  `turn_count` against the turns held locally (and the count last reconciled against,
 *  `serverTurnCount`) is the cheap signal; the turn read then decides what is missing.
 *  A copy with no durable turn of its own (a send that never got a task) has nothing to
 *  anchor a reconcile on and is left alone. */
export function isBehindServer(session: ChatSession, summary: DurableChatSession): boolean {
  const held = localTurnIds(session.messages);
  if (!held.size) return false;
  // The exact signal: the server's newest turn is one this copy has never held (nor
  // already reconciled against). A count can't see that once this copy remembers turns
  // the store has swept — 13 local turns against 11 durable ones read "not behind" while
  // the newest durable one was missing.
  const newest = summary.last_task_id;
  if (newest && !held.has(newest) && session.serverLastTaskId !== newest) return true;
  if (typeof summary.turn_count !== "number") return false;
  return summary.turn_count > Math.max(held.size, session.serverTurnCount ?? 0);
}

/** Does the server's newest turn name a task this copy still shows STREAMING? A
 *  server-started turn that finishes while the console is away leaves its live preview
 *  spinning with no producer left (bd-wk4ti): the bus events that settle it were missed,
 *  and the preview itself holds the newest task id — so `isBehindServer` reads "in step"
 *  and the session was never re-read. This is the cheap index-level signal; the turns read
 *  then decides (reconcileServerTurns finalizes only what the rows report terminal). A
 *  PAUSED bubble is parked on the operator, not stranded — its turn really is not over. */
export function hasStaleStreamingTurn(session: ChatSession, summary: DurableChatSession): boolean {
  const newest = summary.last_task_id;
  if (!newest) return false;
  return session.messages.some(
    (message) =>
      isOwnTurnBubble(message) && message.status === "streaming" && !message.paused && message.taskId === newest,
  );
}

/** A stranded streaming bubble, settled the way the durable terminal branch settles one:
 *  done / error / stopped by the server's state for its task, running tool cards closed.
 *  It keeps what it already showed — the turn's durable copy holds the authoritative rest. */
function finalizeStrandedBubble(message: ChatMessage, state: string | undefined): ChatMessage {
  if (isTaskCanceled(state)) return stopBubble(message);
  return {
    ...message,
    status: isTaskFailed(state) ? "error" : "done",
    paused: undefined,
    durableSnapshotFallback: undefined,
    toolCalls: message.toolCalls?.map((call) =>
      call.status === "running" ? { ...call, status: "done" as const } : call,
    ),
  };
}

/** Reconcile a non-empty local transcript with the server's turns, server-authoritative
 *  for what the local copy never saw while keeping what it did:
 *
 *  - Every server turn AFTER the newest turn the local copy holds is added, in the
 *    server's drawing order (planDurableTurns; a queued turn as its prompt alone). An
 *    older server turn the local copy lacks is left out — it was there when this copy was
 *    written, so its absence is local intent (a regenerate), not staleness.
 *  - A local turn that is only a durable SNAPSHOT (`durableSnapshotFallback`, written by an
 *    earlier hydration while the turn was still running) is redrawn from the server's
 *    current copy; it never held anything richer than the server's own.
 *  - Locally rendered turns are kept as they are (their ordered parts and annotations).
 *
 *  Returns the new transcript, null when there is nothing to change, or "retry" while a
 *  local turn is still LIVE in this copy (streaming, not a snapshot): its reattach owns it,
 *  and adding turns after it would strand it mid-transcript — the next boot reconciles.
 *
 *  LIVE means live on the server too. A bubble still streaming a task the server reports
 *  TERMINAL — and not its live turn — has no producer left: the turn finished while this
 *  console was away and the bus events that settle its preview were missed (bd-wk4ti).
 *  Retrying on it spun it forever, while the turn's durable copy was drawn as a second
 *  bubble beside it. Exactly one bubble per task instead: the turn is redrawn from the
 *  server's copy over the placeholder, or — when the turn also has locally rendered
 *  bubbles to keep — the placeholder alone is finalized in place. */
export function reconcileServerTurns(
  local: ChatMessage[],
  rows: DurableChatTurn[],
  liveTaskId?: string | null,
): ChatMessage[] | null | "retry" {
  const { turns, queued } = planDurableTurns(rows, liveTaskId);
  const known = localTurnIds(local);
  let anchor = -1;
  turns.forEach((turn, index) => {
    if (known.has(turn.task_id)) anchor = index;
  });
  if (anchor < 0) return null;
  // Settled by the server: nothing will ever stream into this bubble again. Only with a
  // marking server (`live_task_id` present, so null is an explicit "nothing live here") —
  // an older server cannot vouch, and the bubble keeps its "retry" protection.
  const serverStates = new Map(rows.map((row) => [row.task_id, row.state]));
  const settledByServer = (message: ChatMessage): boolean =>
    message.status === "streaming" &&
    liveTaskId !== undefined &&
    message.taskId !== liveTaskId &&
    isTaskTerminal(serverStates.get(message.taskId ?? ""));
  if (
    local.some(
      (message) =>
        isOwnTurnBubble(message) &&
        message.status === "streaming" &&
        !message.durableSnapshotFallback &&
        !settledByServer(message),
    )
  ) {
    return "retry";
  }
  const draw = (turn: DurableChatTurn) =>
    queued.has(turn.task_id)
      ? messagesFromDurableTurn(turn).filter((message) => message.role === "user")
      : messagesFromDurableTurn(turn);
  let messages = local;
  let changed = false;
  for (const turn of turns.slice(0, anchor + 1)) {
    const bubbles = messages.filter((message) => isOwnTurnBubble(message) && message.taskId === turn.task_id);
    if (!bubbles.length || !bubbles.every((message) => message.durableSnapshotFallback || settledByServer(message))) continue;
    const fresh = draw(turn);
    const freshIds = new Set(fresh.map((message) => message.id));
    const stale = (message: ChatMessage) =>
      (message.id !== undefined && freshIds.has(message.id)) ||
      (isOwnTurnBubble(message) && message.taskId === turn.task_id);
    const at = messages.findIndex(stale);
    const before = messages.slice(0, at);
    const after = messages.slice(at).filter((message) => !stale(message));
    messages = [...before, ...fresh, ...after];
    changed = true;
  }
  const missing = turns.slice(anchor + 1).filter((turn) => !known.has(turn.task_id));
  if (missing.length) {
    messages = [...messages, ...missing.flatMap(draw)];
    changed = true;
  }
  // Defensive guard (bd-wk4ti): any bubble still streaming a server-settled turn that the
  // redraw above did not cover — a placeholder sharing its turn with locally rendered
  // bubbles (a split preview's live half), or a server-turn preview that never learned its
  // task id (`server-turn-<session>`), which an explicit "nothing live here" settles — is
  // finalized in place so its spinner stops. An EMPTY task-less preview has nothing of its
  // own to keep and is dropped: its turn's durable copy is the one bubble that remains.
  const stranded = (message: ChatMessage): boolean =>
    (isOwnTurnBubble(message) && settledByServer(message)) ||
    (message.role === "assistant" &&
      !message.author &&
      !message.taskId &&
      message.status === "streaming" &&
      liveTaskId === null &&
      !!message.id?.startsWith("server-turn-"));
  if (messages.some(stranded)) {
    messages = messages
      .filter((message) => !(stranded(message) && !message.taskId && isEmptyPlaceholder(message)))
      .map((message) =>
        stranded(message) ? finalizeStrandedBubble(message, serverStates.get(message.taskId ?? "")) : message,
      );
    changed = true;
  }
  return changed ? dedupeMessages(messages) : null;
}

export const SESSION_INDEX_LIMIT = 50;
/** At most this many PARKED sessions are pinned into the index (#3957) — the rest of the
 *  SESSION_INDEX_LIMIT stays the newest sessions, so many parked chats never crowd them out
 *  or grow the load past the cap. */
export const SESSION_PARKED_LIMIT = 20;
export const SESSION_TURN_LIMIT = 50;
export const HYDRATION_CONCURRENCY = 4;

/** The sessions a fresh profile hydrates (#3957): every PARKED session the server named
 *  (up to SESSION_PARKED_LIMIT), then the newest ones, SESSION_INDEX_LIMIT in all.
 *
 *  The newest-N index alone can leave out an older session still waiting on an `ask_human`
 *  answer or an approval, and its question never came back as a tab. A parked row counts
 *  only when it says it is parked (`last_state`): a server that predates the `parked` index
 *  ignores the flag and serves plain newest rows, which must not displace anything. */
export function pinParkedSessions(
  newest: DurableChatSession[],
  parked: DurableChatSession[],
  limit = SESSION_INDEX_LIMIT,
): DurableChatSession[] {
  const pinned = parked.filter((row) => isTaskPaused(row.last_state ?? "")).slice(0, SESSION_PARKED_LIMIT);
  const seen = new Set<string>();
  const out: DurableChatSession[] = [];
  for (const row of [...pinned, ...newest]) {
    if (seen.has(row.session_id)) continue;
    seen.add(row.session_id);
    out.push(row);
  }
  return out.slice(0, limit);
}

// session_id → the highest `last_change_seq` the index has reported for it (ADR 0130 D3).
// The visibility backstop: a session whose server seq has climbed past what this console last
// rendered is refetched even when its turn count and newest task id are unchanged (a rename, a
// status flip). Seeded on first sight so a freshly booted index does not re-read everything.
const changeSeqSeen = new Map<string, number>();

/** A locally-held session the server's change feed has moved past since this console last read
 *  it — the `last_change_seq` backstop (D3). False on first sight (nothing to compare), on a
 *  server that sends no seq, and when the seq is unchanged. */
function behindByChangeSeq(summary: DurableChatSession): boolean {
  const seq = summary.last_change_seq;
  if (typeof seq !== "number" || !Number.isFinite(seq)) return false;
  const seen = changeSeqSeen.get(summary.session_id);
  return seen !== undefined && seq > seen;
}

/** Remember the index's newest `last_change_seq` for a session, monotonically. */
function recordChangeSeq(summary: DurableChatSession): void {
  const seq = summary.last_change_seq;
  if (typeof seq !== "number" || !Number.isFinite(seq)) return;
  const seen = changeSeqSeen.get(summary.session_id);
  if (seen === undefined || seq > seen) changeSeqSeen.set(summary.session_id, seq);
}

/** Fetch only server-only, locally empty, render-stale, or BEHIND-the-server sessions
 * (isBehindServer or the `last_change_seq` backstop), with bounded fan-out. `full` forces a
 * re-read of EVERY locally-held session's turns — the `resync_required` response (ADR 0130
 * D3), where a cursor gap past retention means no incremental signal can be trusted.
 * Every read is best-effort: an offline/cold fleet member leaves local chat
 * untouched and will be tried again on the next full page boot. */
export async function hydrateDurableChatSessions(opts: { full?: boolean } = {}): Promise<void> {
  let summaries: DurableChatSession[];
  try {
    const [newest, parked] = await Promise.all([
      api.chatSessions(SESSION_INDEX_LIMIT),
      // Best-effort: without it the newest index still hydrates, as before.
      api.chatSessions(SESSION_PARKED_LIMIT, { parked: true }).catch(() => ({ sessions: [] })),
    ]);
    summaries = pinParkedSessions(newest.sessions, parked.sessions ?? []);
  } catch {
    return;
  }
  const eligibility = new Map<string, HydrationEligibility>();
  const local = new Map(chatStore.getSnapshot().sessions.map((session) => [session.id, session]));
  const wanted = summaries.filter((summary) => {
    const session = local.get(summary.session_id);
    // A non-empty local copy is fetched to repair a stripped render (#3340), because the
    // server has turns it never saw — another client's, after this cache was written — or
    // because a turn it still shows STREAMING is the server's newest (bd-wk4ti): a stranded
    // server-turn preview holds that task id itself, which reads "in step" to isBehindServer.
    // A turn THIS window is running (localTurnInFlight) is its own producer's to settle.
    // The `last_change_seq` backstop (D3) catches a change that moved neither — a rename, a
    // status flip — and `full` re-reads every held session (resync_required).
    const behind = Boolean(
      session?.messages.length &&
        !needsDurableHydration(session) &&
        (opts.full ||
          isBehindServer(session, summary) ||
          (!localTurnInFlight(summary.session_id) && hasStaleStreamingTurn(session, summary)) ||
          behindByChangeSeq(summary)),
    );
    if (session?.messages.length && !needsDurableHydration(session) && !behind) return false;
    const token = chatStore.captureHydrationEligibility(summary.session_id, { behind });
    if (!token) return false;
    eligibility.set(summary.session_id, token);
    return true;
  });
  // Record the index's seqs AFTER the behind decision read the prior values, so the next
  // visibility check compares against what the server reports now.
  summaries.forEach(recordChangeSeq);
  const hydrated: ChatSession[] = [];
  const reconciled: SessionReconcile[] = [];
  let cursor = 0;
  async function worker() {
    while (cursor < wanted.length) {
      const summary = wanted[cursor++];
      try {
        const { turns, live_task_id } = await api.chatSessionTurns(summary.session_id, SESSION_TURN_LIMIT);
        const token = eligibility.get(summary.session_id);
        if (token?.behind && token.localSession) {
          const messages = reconcileServerTurns(token.localSession.messages, turns, live_task_id);
          if (messages !== "retry") {
            reconciled.push({
              token,
              messages,
              serverTurnCount: summary.turn_count,
              serverLastTaskId: summary.last_task_id,
            });
          }
          continue;
        }
        const session = sessionFromDurableTurns(summary, turns, live_task_id);
        if (session) hydrated.push(session);
      } catch {
        // One session failing must not discard successful siblings.
      }
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(HYDRATION_CONCURRENCY, wanted.length) }, () => worker()),
  );
  if (reconciled.length) {
    chatStore.reconcileSessions(reconciled);
    // A reconcile can settle the session's LAST streaming bubble (a stranded server-turn
    // preview, bd-wk4ti): hand its "streaming" back when nothing else still runs it, or
    // the composer stays locked on a turn that is over. The reconciler itself checks the
    // live claims and remaining streaming bubbles, so this is safe however often it runs.
    for (const update of reconciled) {
      if (update.messages) reconcileSessionStatus(update.token.sessionId);
    }
  }
  if (hydrated.length) {
    chatStore.hydrateSessions(
      hydrated,
      hydrated.flatMap((session) => {
        const token = eligibility.get(session.id);
        return token ? [token] : [];
      }),
    );
  }
}

/** How often a visible console re-reads the session index while it stays open. */
export const RESYNC_INTERVAL_MS = 60_000;
/** The least time between two re-syncs, however often the window regains focus. */
export const RESYNC_MIN_GAP_MS = 10_000;

let resyncInFlight: Promise<void> | null = null;
let lastResyncAt = 0;

/** Re-run durable hydration while the console stays open (ADR 0104 multi-device catch-up).
 *  Boot hydration alone left an open window blind to turns another device ran — the
 *  phone's replies never reached a desktop window that stayed open all day. Single-flight
 *  and spaced by RESYNC_MIN_GAP_MS; `force` skips the spacing (not the single flight).
 *  `full` re-reads every held session's turns (the `resync_required` response, ADR 0130 D3).
 *  Every commit still goes through the eligibility veto, so a local edit made while the
 *  read is out wins. */
export function resyncDurableChatSessions(
  opts: { force?: boolean; now?: number; full?: boolean } = {},
): Promise<void> {
  if (resyncInFlight) return resyncInFlight;
  const now = opts.now ?? Date.now();
  if (!opts.force && now - lastResyncAt < RESYNC_MIN_GAP_MS) return Promise.resolve();
  lastResyncAt = now;
  resyncInFlight = hydrateDurableChatSessions({ full: opts.full }).finally(() => {
    resyncInFlight = null;
  });
  return resyncInFlight;
}

/** The change-feed topics (ADR 0130 D2) that mean "a chat session or one of its turns moved".
 *  Events carry identities, not payloads — the console reacts by refetching the affected
 *  entities (the session index and any open session the server has moved past), never by
 *  trusting the frame's body. */
export const SYNC_TOPICS = [
  "session.changed",
  "turn.started",
  "turn.completed",
  "turn.failed",
  "turn.finished",
  "turn.resumed",
] as const;

// Coalesce a burst of change-feed frames into one forced resync: while one is in flight,
// remember that more arrived and run exactly once more after it settles — so a turn that
// fires `turn.completed` + two `session.changed` in a row costs one extra read, not three.
let syncQueued = false;
let syncFull = false;
async function drainServerChange(): Promise<void> {
  do {
    const full = syncFull;
    syncQueued = false;
    syncFull = false;
    await resyncDurableChatSessions({ force: true, full });
  } while (syncQueued);
}

/** React to one change-feed frame: refetch the entities it names (D2). `full` (the
 *  `resync_required` path) re-reads every open session's turns. */
export function onServerChange(opts: { full?: boolean } = {}): void {
  if (opts.full) syncFull = true;
  if (resyncInFlight) {
    syncQueued = true;
    return;
  }
  void drainServerChange();
}

/** Subscribe the console to the server's change feed (ADR 0130 D3): a `session.changed` or
 *  `turn.*` frame routes to an entity refetch, and `resync_required` (a cursor gap past
 *  retention) to a full refetch of the session list and every open session's turns. Returns
 *  the combined unsubscribe. */
export function watchServerChangeFeed(): () => void {
  const offs = SYNC_TOPICS.map((topic) => onTopic(topic, () => onServerChange()));
  offs.push(onTopic("resync_required", () => onServerChange({ full: true })));
  return () => offs.forEach((off) => off());
}

/** Keep an open console in step with the server: subscribe to the live change feed (ADR 0130
 *  D3) so another device's send, answer or background result refetches here within a beat,
 *  and — as a backstop — re-sync when the window comes back (visibility, focus) and on a slow
 *  interval while it is visible. The visibility re-sync compares each open session's
 *  `last_change_seq` with the server's, catching anything the feed missed while the stream was
 *  down. A hidden window does nothing; it catches up the moment it is shown. Returns the
 *  unsubscribe. */
export function watchDurableSessions(): () => void {
  const offFeed = watchServerChangeFeed();
  if (typeof document === "undefined" || typeof window === "undefined") return offFeed;
  const visible = () => document.visibilityState !== "hidden";
  const onReturn = () => {
    if (visible()) void resyncDurableChatSessions();
  };
  const timer = window.setInterval(onReturn, RESYNC_INTERVAL_MS);
  document.addEventListener("visibilitychange", onReturn);
  window.addEventListener("focus", onReturn);
  return () => {
    offFeed();
    window.clearInterval(timer);
    document.removeEventListener("visibilitychange", onReturn);
    window.removeEventListener("focus", onReturn);
  };
}

/** Test seam: forget the spacing, any in-flight re-sync, the change-feed coalescer, and the
 *  `last_change_seq` backstop. */
export function resetDurableResyncForTests(): void {
  resyncInFlight = null;
  lastResyncAt = 0;
  syncQueued = false;
  syncFull = false;
  changeSeqSeen.clear();
}
