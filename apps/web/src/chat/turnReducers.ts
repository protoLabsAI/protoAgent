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
      parentId: evt.parentId ?? openTask?.id,
    };
    calls.push(card);
    if (card.parentId == null) nextParts = addToolRef(message.parts, evt.id);
  } else {
    // end — flip the matching card to done/error (or create one if the start
    // frame was missed). A failed end (e.g. a declined run_command) closes the
    // card as an error (X). Stamp elapsed when we saw the start.
    const startedAt = idx >= 0 ? calls[idx].startedAt : undefined;
    const durationMs = startedAt !== undefined ? now - startedAt : undefined;
    const endStatus = evt.error ? ("error" as const) : ("done" as const);
    if (idx >= 0) {
      calls[idx] = { ...calls[idx], output: evt.output, outputChars: evt.outputChars, status: endStatus, durationMs, paused: undefined };
    } else {
      // Missed start — treat as a fresh top-level call so it still renders.
      calls.push({ id: evt.id, name: evt.name, output: evt.output, outputChars: evt.outputChars, status: endStatus });
      nextParts = addToolRef(message.parts, evt.id);
    }
  }
  return { ...message, toolCalls: calls, parts: nextParts };
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
          durationMs: c.durationMs ?? (c.startedAt !== undefined ? now - c.startedAt : undefined),
        }
      : c.paused
        ? { ...c, paused: undefined }
        : c,
  );
  return { ...message, status: "done", paused: undefined, toolCalls };
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
