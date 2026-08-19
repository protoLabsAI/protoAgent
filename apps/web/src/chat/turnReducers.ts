// Pure per-message reducers for a streaming turn's events — extracted from the
// send path's inline closures (Swap & Resume S1) so the LIVE stream and the
// REATTACH stream (tasks/resubscribe after an agent switch / reload) apply
// text, reasoning, tool, and component frames with byte-identical semantics.
// Each takes the assistant ChatMessage and returns the next one; callers map
// over the session's messages.

import type { ChatMessage, ComponentSpec, ToolCall, ToolEvent, TurnUsage } from "../lib/types";
import { addComponent, addToolRef, appendReasoning, appendText, replaceText } from "./parts";

export function applyText(message: ChatMessage, text: string, append: boolean): ChatMessage {
  return {
    ...message,
    content: append ? `${message.content}${text}` : text,
    // A replace spans the WHOLE turn's text (the terminal frame re-sends the
    // full canonical answer, preamble included) — replaceText keeps the
    // streamed interleaving when nothing diverged and rebuilds otherwise;
    // appendText's open-run rewrite would double a pre-tool preamble.
    parts: append ? appendText(message.parts, text, true) : replaceText(message.parts, text, message.content),
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
  if (evt.phase === "start") {
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
    if (idx >= 0) calls[idx] = { ...calls[idx], ...card };
    else calls.push(card);
    if (card.parentId == null) nextParts = addToolRef(message.parts, evt.id);
  } else {
    // end — flip the matching card to done/error (or create one if the start
    // frame was missed). A failed end (e.g. a declined run_command) closes the
    // card as an error (X). Stamp elapsed when we saw the start.
    const startedAt = idx >= 0 ? calls[idx].startedAt : undefined;
    const durationMs = startedAt !== undefined ? now - startedAt : undefined;
    const endStatus = evt.error ? ("error" as const) : ("done" as const);
    if (idx >= 0) {
      calls[idx] = { ...calls[idx], output: evt.output, outputChars: evt.outputChars, status: endStatus, durationMs };
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
