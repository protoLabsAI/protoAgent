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
  DEFAULT_SESSION_TITLE,
  needsDurableHydration,
  type ChatSession,
  type HydrationEligibility,
} from "./chat-store";
import { applyDelegateProgressTo } from "./delegateProgress";
import { rendersText, replaceText, textRuns } from "./parts";
import { isEmptyPlaceholder } from "./roomBubble";
import { isTaskFailed, isTaskPaused, isTaskTerminal } from "./taskState";
import { applyComponent, applyReasoning, applyText, applyToolEvent, applyUsage, pauseBubble } from "./turnReducers";

function timestamp(value: string | null): number {
  const parsed = value ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : Date.now();
}

/** The instant the store recorded, or `undefined` when it has none. A MESSAGE's `createdAt`
 *  is shown to the operator as when it was sent, so it is never the hydration-time "now"
 *  `timestamp` falls back to for session ordering. */
function recordedAt(value: string | null): number | undefined {
  const parsed = value ? Date.parse(value) : NaN;
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
  const messages: ChatMessage[] = prompt
    ? [{ id: `durable-${turn.task_id}-user`, role: "user", content: prompt, status: "done" }]
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
  replayDurableChatTurn(turn, "", {
    onText: (text, append) => {
      assistant = applyText(assistant, text, append);
    },
    onSteerConsumed: (items) => {
      // The agent read the operator's interjection HERE — between the work above and
      // the work below — so the rebuilt transcript splits there, exactly as the live
      // one does (roomBubble.insertConversationBubbles). The ANSWER lands on the
      // trailing bubble: the durable artifacts flatten every text frame into one
      // accumulation, so how much of the prose preceded the interjection is not
      // recoverable. Ids are the steer's own, so a later live settle is a no-op
      // (steerPlacement.placeConsumedSteers dedupes on them).
      if (!isEmptyPlaceholder(assistant)) {
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
      if (!isEmptyPlaceholder(assistant)) {
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
      // An `@` mention card's coding delegate, at its final (durably kept) state (#3975).
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
    assistant = {
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
    if (turn.text && assistant.parts?.length && !rendersText(textRuns(assistant.parts), turn.text)) {
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
  if (settled.length && terminal && isEmptyPlaceholder(assistant) && !assistant.components?.length) {
    return [...messages, ...settled];
  }
  return [...messages, ...settled, assistant];
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
  const firstPrompt = messages.find((message) => message.role === "user")?.content ?? "";
  return {
    id: summary.session_id,
    title: titleFromPrompt(firstPrompt),
    messages,
    createdAt,
    updatedAt,
    ...(durableIncognito(turns) ? { incognito: true } : {}),
  };
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

/** Fetch only server-only or locally empty sessions, with bounded fan-out.
 * Every read is best-effort: an offline/cold fleet member leaves local chat
 * untouched and will be tried again on the next full page boot. */
export async function hydrateDurableChatSessions(): Promise<void> {
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
    if (session?.messages.length && !needsDurableHydration(session)) return false;
    const token = chatStore.captureHydrationEligibility(summary.session_id);
    if (!token) return false;
    eligibility.set(summary.session_id, token);
    return true;
  });
  const hydrated: ChatSession[] = [];
  let cursor = 0;
  async function worker() {
    while (cursor < wanted.length) {
      const summary = wanted[cursor++];
      try {
        const { turns, live_task_id } = await api.chatSessionTurns(summary.session_id, SESSION_TURN_LIMIT);
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
