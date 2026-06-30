import type { ChatPart, ComponentSpec, ToolCall } from "../lib/types";

// Ordered-parts accumulation for a streaming assistant turn. These keep the
// emission order of reasoning, answer text and tool calls so a pre-tool preamble
// renders above the tool cards and post-tool text below them, and "thinking"
// renders inline next to the step it precedes (instead of the old layout that
// hoisted reasoning to the top and grouped all text after all tool cards). Pure +
// unit-tested (parts.test.ts).

/** Append a streamed text delta to the ordered parts. Extends the open text run,
 *  or starts a new one when the previous block was a tool group (so post-tool text
 *  renders below the cards). `append=false` replaces the open run (the terminal,
 *  non-streamed answer) or starts the first run. */
export function appendText(parts: ChatPart[] | undefined, text: string, append: boolean): ChatPart[] {
  const next = [...(parts ?? [])];
  const last = next[next.length - 1];
  if (last?.kind === "text") {
    next[next.length - 1] = { kind: "text", text: append ? last.text + text : text };
    return next;
  }
  // Starting a NEW text run (first part, or after a tool group). Drop leading
  // whitespace: a stray "\n" the model emits between two tool calls would otherwise
  // become its own text part — rendering an empty markdown block (a visible gap) AND
  // splitting the tool group so the next call can't extend it. Pure whitespace ⇒ skip
  // entirely, keeping the tool group open.
  const trimmed = text.replace(/^\s+/, "");
  if (!trimmed) return next;
  next.push({ kind: "text", text: trimmed });
  return next;
}

/** Append a streamed reasoning ("thinking") delta to the ordered parts. Extends the
 *  open reasoning run, or starts a new one when the previous block was text/tools — so
 *  thinking that resumes between tool calls renders inline at that point rather than
 *  hoisted to the top. Leading whitespace is dropped on a new run (same empty-block
 *  guard as appendText). Always streamed, so it only ever appends. */
export function appendReasoning(parts: ChatPart[] | undefined, text: string): ChatPart[] {
  const next = [...(parts ?? [])];
  const last = next[next.length - 1];
  if (last?.kind === "reasoning") {
    next[next.length - 1] = { kind: "reasoning", text: last.text + text };
    return next;
  }
  const trimmed = text.replace(/^\s+/, "");
  if (!trimmed) return next;
  next.push({ kind: "reasoning", text: trimmed });
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

/** Append an inline component as an ordered part — at its emission point, so it renders
 *  ABOVE the answer text that streams in after it (#1323). */
export function addComponent(parts: ChatPart[] | undefined, spec: ComponentSpec): ChatPart[] {
  return [...(parts ?? []), { kind: "component", spec }];
}

/** Split a turn's parts into the folded "work" (the reason→tool→interstitial timeline behind the
 *  WorkBlock) and the trailing "answer" (the final text/component run rendered below it).
 *
 *  `fold` is true for a reason+tool turn — reasoning AND a tool call (the pre-#1417 condition):
 *  the WorkBlock keeps the streaming view clean — just "Working… [tally]" + the running-tool
 *  spotlight — and folds the reasoning, interstitial narration, and multi-batch tool timeline
 *  behind one (collapsed, expandable) disclosure. That's what stops a chatty reason+tool turn from
 *  flashing interim narration into the main chat as the agent thinks/narrates between calls.
 *  A tool-only turn (no reasoning), reasoning-only, and plain text don't fold — they stream their
 *  parts inline, so a simple tool result still shows its card directly (not hidden behind "Worked").
 *
 *  Settle guard: WHILE STREAMING a folded turn, keep EVERYTHING as work (nothing renders below the
 *  WorkBlock); only once the turn settles (`!streaming`) do we split the final text/component run
 *  out as the answer beneath the collapsed "Worked" summary. Promoting a trailing run eagerly made
 *  interstitial narration flash into the main chat, then jump back into the timeline when the next
 *  tool arrived. */
export function foldPlan(
  parts: ChatPart[],
  streaming: boolean,
): { fold: boolean; workParts: ChatPart[]; answerParts: ChatPart[] } {
  let split = parts.length;
  while (split > 0 && (parts[split - 1].kind === "text" || parts[split - 1].kind === "component")) split--;
  const baseWork = parts.slice(0, split);
  const fold = baseWork.some((p) => p.kind === "tools") && baseWork.some((p) => p.kind === "reasoning");
  if (fold && streaming) return { fold, workParts: parts, answerParts: [] };
  return { fold, workParts: baseWork, answerParts: parts.slice(split) };
}

/** The tool calls to render for a `tools` part: its top-level calls (by id) plus any
 *  subagent children nested under them — so ToolCalls can rebuild the nesting. */
export function toolsForGroup(ids: string[], calls: ToolCall[] | undefined): ToolCall[] {
  const set = new Set(ids);
  return (calls ?? []).filter((c) => set.has(c.id) || (c.parentId != null && set.has(c.parentId)));
}
