import type { ChatPart, ToolCall } from "../lib/types";

// Ordered-parts accumulation for a streaming assistant turn. These keep the
// emission order of answer text and tool calls so a pre-tool preamble renders
// above the tool cards and post-tool text below them (instead of the old layout
// that grouped all text after all tool cards). Pure + unit-tested (parts.test.ts).

/** Append a streamed text delta to the ordered parts. Extends the open text run,
 *  or starts a new one when the previous block was a tool group (so post-tool text
 *  renders below the cards). `append=false` replaces the open run (the terminal,
 *  non-streamed answer) or starts the first run. */
export function appendText(parts: ChatPart[] | undefined, text: string, append: boolean): ChatPart[] {
  const next = [...(parts ?? [])];
  const last = next[next.length - 1];
  if (last?.kind === "text") {
    next[next.length - 1] = { kind: "text", text: append ? last.text + text : text };
  } else {
    next.push({ kind: "text", text });
  }
  return next;
}

/** Record a new TOP-LEVEL tool call in emission order: extend the current tool
 *  group if the last block is one, else open a new group after the preceding text.
 *  Child calls (parentId set) don't open a block — they nest under their parent's
 *  card via `toolCalls` at render time, so only pass top-level ids here. */
export function addToolRef(parts: ChatPart[] | undefined, id: string): ChatPart[] {
  const next = [...(parts ?? [])];
  const last = next[next.length - 1];
  if (last?.kind === "tools") {
    if (!last.ids.includes(id)) next[next.length - 1] = { kind: "tools", ids: [...last.ids, id] };
    return next;
  }
  next.push({ kind: "tools", ids: [id] });
  return next;
}

/** The tool calls to render for a `tools` part: its top-level calls (by id) plus any
 *  subagent children nested under them — so ToolCalls can rebuild the nesting. */
export function toolsForGroup(ids: string[], calls: ToolCall[] | undefined): ToolCall[] {
  const set = new Set(ids);
  return (calls ?? []).filter((c) => set.has(c.id) || (c.parentId != null && set.has(c.parentId)));
}
