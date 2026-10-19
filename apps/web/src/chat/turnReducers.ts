// Pure per-message reducers for a streaming turn's events — extracted from the
// send path's inline closures (Swap & Resume S1) so the LIVE stream and the
// REATTACH stream (tasks/resubscribe after an agent switch / reload) apply
// text, reasoning, tool, and component frames with byte-identical semantics.
// Each takes the assistant ChatMessage and returns the next one; callers map
// over the session's messages.

import type { ChatMessage, ComponentSpec, ToolCall, ToolEvent, TurnUsage } from "../lib/types";
import type { ToolArgsEvent } from "../lib/api/a2aStream";
import { addComponent, addToolRef, appendReasoning, appendText, replaceText } from "./parts";
import { isTaskPaused } from "./taskState";
import { appendToolArgs, emptyToolArgs, toToolArgsBuffer } from "./toolArgsBuffer";
import type { ToolArgsBuffer, ToolArgsBuffers } from "./toolArgsBuffer";

export function applyText(message: ChatMessage, text: string, append: boolean): ChatMessage {
  return {
    ...message,
    content: append ? `${message.content}${text}` : text,
    // A replace spans the WHOLE turn's text (the terminal frame re-sends the
    // full canonical answer, preamble included) — replaceText keeps the
    // streamed interleaving when nothing diverged and rebuilds otherwise;
    // appendText's open-run rewrite would double a pre-tool preamble.
    parts: append ? appendText(message.parts, text, true) : replaceText(message.parts, text),
    status: "streaming",
  };
}

export function applyReasoning(message: ChatMessage, delta: string): ChatMessage {
  // Accumulate the streamed scratch_pad two ways: into `reasoning` (the flat
  // block kept for history/persistence) AND into the ordered `parts`, so
  // thinking renders inline at the point it occurred.
  return {
    ...message,
    reasoning: `${message.reasoning ?? ""}${delta}`,
    parts: appendReasoning(message.parts, delta),
  };
}

/** A card's elapsed time at `now`, or undefined when it cannot be known: the call never had
 *  a start stamp, or that stamp is a snapshot REPLAY's clock rather than the call's own
 *  (#4076) — `now - startedAt` would then show a fake "0ms" (or the time since a reload). */
export function elapsedSince(call: ToolCall, now: number): number | undefined {
  if (call.startedAt === undefined || call.startReplayed) return undefined;
  return now - call.startedAt;
}

export function applyToolEvent(message: ChatMessage, evt: ToolEvent): ChatMessage {
  const calls = [...(message.toolCalls || [])];
  const idx = calls.findIndex((c) => c.id === evt.id);
  const now = Date.now();
  // Ordered render blocks: a top-level tool opens/extends a tool group in
  // emission order; children (parentId set) nest under their parent's card,
  // so they don't get their own block.
  let nextParts = message.parts;
  if (evt.phase === "start" && idx >= 0) {
    // A re-announce of a card we already have (the native runtime's second start with
    // full args, an ACP coder's refined name/args — #3691): fill it in. Keep its clock,
    // nesting and position — re-adding the ref would render the card twice once text
    // arrived in between, and resetting startedAt would shorten its elapsed time.
    calls[idx] = { ...calls[idx], name: evt.name || calls[idx].name, input: evt.input ?? calls[idx].input };
  } else if (evt.phase === "start") {
    // Nest a subagent's own tool under its `task` card. The server tags the
    // child frame with the parent delegation's id (authoritative — works even
    // though the task's end races AHEAD of the child); fall back to "last open
    // task wins" only for older servers that don't send it.
    const openTask = [...calls].reverse().find((c) => c.name === "task" && c.status === "running" && c.id !== evt.id);
    const card: ToolCall = {
      id: evt.id,
      name: evt.name,
      input: evt.input,
      status: "running",
      startedAt: now,
      ...(evt.replayed ? { startReplayed: true } : {}),
      parentId: evt.parentId ?? openTask?.id,
    };
    calls.push(card);
    if (card.parentId == null) nextParts = addToolRef(message.parts, evt.id);
  } else {
    // end — flip the matching card to done/error (or create one if the start
    // frame was missed). A failed end (e.g. a declined run_command) closes the
    // card as an error (X). The duration is the server's measured one when the frame carries
    // it; else the client's own start→end, but ONLY when both frames were seen live — a
    // replayed pair is microseconds apart and would read a fake "0ms" (#4076).
    const durationMs = evt.durationMs ?? (idx >= 0 && !evt.replayed ? elapsedSince(calls[idx], now) : undefined);
    const endStatus = evt.error ? ("error" as const) : ("done" as const);
    if (idx >= 0) {
      calls[idx] = { ...calls[idx], output: evt.output, outputChars: evt.outputChars, status: endStatus, durationMs, paused: undefined };
    } else {
      // Missed start — treat as a fresh top-level call so it still renders.
      calls.push({ id: evt.id, name: evt.name, output: evt.output, outputChars: evt.outputChars, status: endStatus, durationMs });
      nextParts = addToolRef(message.parts, evt.id);
    }
  }
  return { ...message, toolCalls: calls, parts: nextParts };
}

/** Apply one tool frame to the TURN, not just its live bubble (#4300).
 *
 *  A turn can be split mid-step: a `delegate_to` room bubble or a consumed steer freezes
 *  what the live bubble holds as a `splitOf` half and resets the live bubble empty
 *  (roomBubble.insertConversationBubbles). When the step made parallel calls — a plugin
 *  tool beside two background `delegate_to`s — the sibling's START is in the frozen half,
 *  but its END arrives after the split. Applied to the live bubble alone, that end found no
 *  card ("missed start"), drew a duplicate ✓ row in the continuation, and left the frozen
 *  card spinning forever: the frozen half is already `done`, so no settle ever reached it.
 *
 *  So a frame for a call the live bubble doesn't hold goes to the turn bubble that does —
 *  the end (or a re-announcing start) to the half holding the card, a child's start to the
 *  half holding its parent. Anything else lands on the live bubble, as before. */
export function applyToolEventToTurn(messages: ChatMessage[], assistantId: string, evt: ToolEvent): ChatMessage[] {
  const holds = (m: ChatMessage | undefined, id: string | undefined) =>
    id != null && Boolean(m?.toolCalls?.some((c) => c.id === id));
  // Latest half first: a call id is unique per turn, but the newest split is its likeliest home.
  const halfHolding = (id: string | undefined) =>
    [...messages].reverse().find((m) => m.splitOf === assistantId && holds(m, id));
  const live = messages.find((m) => m.id === assistantId);
  let target: ChatMessage | undefined;
  if (!holds(live, evt.id)) {
    target = halfHolding(evt.id);
    if (!target && evt.phase === "start" && !holds(live, evt.parentId)) target = halfHolding(evt.parentId);
  }
  const targetId = target?.id ?? assistantId;
  return messages.map((m) => (m.id === targetId ? applyToolEvent(m, evt) : m));
}

/** Settle the frozen halves of a split turn when the turn ends (#4300): a card still
 *  `running` there can't be — the turn is over — so it settles done (or STOPPED for a
 *  cancelled turn), whatever frame went missing. The halves' own status is untouched;
 *  they were frozen `done` at split time. The live bubble is settled by its own reducer. */
export function settleSplitHalves(
  messages: ChatMessage[],
  assistantId: string,
  opts: { stopped?: boolean; now?: number } = {},
): ChatMessage[] {
  const now = opts.now ?? Date.now();
  let changed = false;
  const next = messages.map((m) => {
    if (m.splitOf !== assistantId || !m.toolCalls?.some((c) => c.status === "running")) return m;
    changed = true;
    return {
      ...m,
      toolCalls: m.toolCalls.map((c) =>
        c.status !== "running"
          ? c
          : opts.stopped
            ? { ...c, status: "done" as const, paused: undefined, stopped: true }
            : { ...c, status: "done" as const, paused: undefined, durationMs: c.durationMs ?? elapsedSince(c, now) },
      ),
    };
  });
  return changed ? next : messages;
}

export function applyComponent(message: ChatMessage, spec: ComponentSpec): ChatMessage {
  // A renderable component (ADR 0051) — an ORDERED part at its emission point
  // so it renders ABOVE the answer text that streams in after (#1323).
  // `components` is the history/persistence fallback for pre-parts messages.
  return {
    ...message,
    parts: addComponent(message.parts, spec),
    components: [...(message.components || []), spec],
  };
}

export function applyUsage(message: ChatMessage, usage: TurnUsage): ChatMessage {
  return { ...message, usage };
}

/** One bubble, paused: the flag, and its running cards marked waiting. */
export function pauseBubble(m: ChatMessage): ChatMessage {
  return {
    ...m,
    paused: true,
    toolCalls: m.toolCalls?.map((c) => (c.status === "running" ? { ...c, paused: true } : c)),
  };
}

/** One bubble, STOPPED (#4076): the turn was cancelled before it finished — an operator Stop
 *  or a `canceled` task state on hydration / reattach. Settles the bubble done (no spinner)
 *  but says so: the bubble gets the footer's "Stopped" marker, and every card still in
 *  flight settles as STOPPED, not done. Flipping those to plain done painted a ✓ on a call
 *  that never returned — after a reload, a cancelled `show_artifact` read as a success with
 *  empty args. A card that already settled keeps its own status. */
/** One bubble, settled off the durable task (the stream watchdog's finalize): done or error,
 *  in-flight cards done with their client-measured duration, and the footer time stamped at the
 *  task's OWN settle instant (`settledAt`, its status timestamp = the `last_updated` a reload
 *  shows) — never the watchdog's "now", which runs ≥45s behind the real end (#4154). `now` is
 *  the fallback for a server that sent no timestamp. */
export function settleBubbleFromTask(
  m: ChatMessage,
  opts: { failed: boolean; settledAt?: number; now: number },
): ChatMessage {
  const toolCalls = m.toolCalls?.map((c) =>
    c.status === "running" ? { ...c, status: "done" as const, durationMs: c.durationMs ?? elapsedSince(c, opts.now) } : c,
  );
  return { ...m, status: opts.failed ? "error" : "done", toolCalls, createdAt: opts.settledAt ?? opts.now };
}

export function stopBubble(m: ChatMessage, opts: { at?: number } = {}): ChatMessage {
  return {
    ...m,
    // A LIVE stop stamps the footer time at the stop — when the turn ended, the same instant a
    // reload reads off the durable turn's `last_updated` — instead of keeping the turn's start
    // time, which made live and reload disagree (#4076). A replayed stop keeps its own.
    ...(opts.at !== undefined ? { createdAt: opts.at } : {}),
    status: "done",
    paused: undefined,
    stopped: true,
    toolCalls: m.toolCalls?.map((c) =>
      c.status === "running" ? { ...c, status: "done" as const, paused: undefined, stopped: true } : c,
    ),
  };
}

/** One bubble, resumed: the pause cleared off it and its cards (the turn continues). */
export function unpauseBubble(m: ChatMessage): ChatMessage {
  if (!m.paused && !m.toolCalls?.some((c) => c.paused)) return m;
  return {
    ...m,
    paused: undefined,
    toolCalls: m.toolCalls?.map((c) => (c.paused ? { ...c, paused: undefined } : c)),
  };
}

/** Whether a turn state is PARKED on the operator — input-required / auth-required. Not
 *  over (the operator's answer continues the same task) and not working either. */
export function isParkedState(state: string | undefined): boolean {
  return isTaskPaused(state);
}

/** The bubble a LIVE stream leaves behind when it closes (#3956).
 *
 *  A turn that PARKED on the operator (an `ask_human` question, a form, an approval) closes
 *  its stream too — the SDK ends `SendStreamingMessage` at an interrupted state — but it is
 *  not over. Settling it "done" like a finished turn lied twice: the in-flight `ask_human`
 *  card flipped to done ✓ while the form was still up, and the persisted transcript said
 *  the turn had ended, so a reload in the same browser had no streaming bubble to reattach
 *  and never brought the form back. A parked turn is left `streaming` and marked paused —
 *  the exact shape a reattach and cold hydration give the same turn (#3946) — so it renders
 *  as waiting now and reattaches to its own task after a reload. The answer that continues
 *  the task settles it (`settleAnsweredPause` / `unpauseBubble`).
 *
 *  Any other end settles the bubble done, flipping a card whose end frame raced the close
 *  (still `running`) to done, with its elapsed time stamped. */
export function settleStreamEnd(message: ChatMessage, opts: { parked: boolean; now?: number }): ChatMessage {
  if (opts.parked) return message.status === "streaming" ? pauseBubble(message) : message;
  const now = opts.now ?? Date.now();
  // Done clears any pause too: a settled bubble never reads as waiting.
  const toolCalls = message.toolCalls?.map((c) =>
    c.status === "running"
      ? {
          ...c,
          status: "done" as const,
          paused: undefined,
          durationMs: c.durationMs ?? elapsedSince(c, now),
        }
      : c.paused
        ? { ...c, paused: undefined }
        : c,
  );
  // The bubble's footer time is when the REPLY landed — the instant a reload reads off the
  // durable turn's `last_updated` — not when the turn started, so live and reload agree (#4076).
  return { ...message, status: "done", paused: undefined, toolCalls, createdAt: now };
}

/** Tracks whether a LIVE stream's turn is parked on the operator (#3956), off the frame
 *  dispatcher's `onInputRequired` / `onTaskState`.
 *
 *  A plugin composer form (#1701) rides the same input-required frame but parks no graph —
 *  its redeem completes the task server-side — so it never counts as parked. That exclusion
 *  leans on the dispatcher's order within one status frame: `onInputRequired` (which carries
 *  the payload, and so the `plugin_callback_id`) fires BEFORE `onTaskState` (lib/api/
 *  a2aStream.ts, pinned by turnReducers.test.ts). The latest state wins: a working state
 *  after a park un-parks. `taskState` returns the transition, or null for none. */
export function createParkTracker() {
  let parked = false;
  let pluginForm = false;
  return {
    get parked() {
      return parked;
    },
    inputRequired(payload: { plugin_callback_id?: string }) {
      if (payload.plugin_callback_id) pluginForm = true;
    },
    taskState(state: string): "parked" | "unparked" | null {
      const next = isParkedState(state) && !pluginForm;
      if (next === parked) return null;
      parked = next;
      return next ? "parked" : "unparked";
    },
  };
}

/** The live turn's streamed tool-argument previews (ADR 0118 D3), keyed by tool-call id — the
 *  per-tool-call buffer `chat/toolArgsBuffer` holds, wired into the turn's live state.
 *
 *  Fed from the frame dispatcher's `onToolArgs` (a2aStream.ts), which decodes `tool-args-v1`
 *  ONLY off live WORKING frames. It is CLEARED when the turn ends — the previews never outlive
 *  their turn — and, being live-only, is never created or populated during hydration or a
 *  reattach replay: the durable store drops tool-args frames from history and snapshot replay
 *  never decodes one, so a reload shows the finished tool-call card, never a stale partial.
 *  Nothing renders it yet — S8 consumes `get`/`all`. */
export function createToolArgsTracker() {
  let buffers: ToolArgsBuffers = emptyToolArgs();
  return {
    /** Fold one decoded tool-args-v1 frame into its tool call's buffer. */
    push(evt: ToolArgsEvent): void {
      buffers = appendToolArgs(buffers, evt);
    },
    /** The current preview for a tool call id, or undefined. */
    get(id: string): ToolArgsBuffer | undefined {
      return toToolArgsBuffer(buffers, id);
    },
    /** Every tool call's preview this turn, keyed by id. */
    all(): Record<string, ToolArgsBuffer> {
      const out: Record<string, ToolArgsBuffer> = {};
      for (const id of Object.keys(buffers)) out[id] = toToolArgsBuffer(buffers, id)!;
      return out;
    },
    /** Drop every preview — the turn ended, and this state never outlives its turn. */
    clear(): void {
      buffers = emptyToolArgs();
    },
    /** How many tool calls have a preview buffered (0 right after `clear`). */
    get size(): number {
      return Object.keys(buffers).length;
    },
  };
}
