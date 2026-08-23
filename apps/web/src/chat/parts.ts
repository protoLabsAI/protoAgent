import type { ChatPart, ComponentSpec, ToolCall } from "../lib/types";

// Ordered-parts accumulation for a streaming assistant turn. These keep the
// emission order of reasoning, answer text and tool calls so a pre-tool preamble
// renders above the tool cards and post-tool text below them, and "thinking"
// renders inline next to the step it precedes (instead of the old layout that
// hoisted reasoning to the top and grouped all text after all tool cards). Pure +
// unit-tested (parts.test.ts).

/** Append a streamed text delta to the ordered parts. Extends the open text run,
 *  or starts a new one when the previous block was a tool group (so post-tool text
 *  renders below the cards). `append=false` replaces the OPEN run only — a full-turn
 *  replace (the terminal A2A frame) must go through `replaceText`, which knows the
 *  replacement spans EVERY text run, not just the trailing one. */
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

/** REPLACE the turn's text with the canonical full-turn `text` (an A2A
 *  artifact-update with `append` absent/false — e.g. the terminal frame, which always
 *  re-sends the whole answer, #1709). The replacement spans EVERY text run — for a
 *  [preamble → tools → answer] turn it includes the pre-tool preamble — so rewriting
 *  only the trailing run would render the preamble twice.
 *
 *  `streamed` is the client's OWN accumulation of this turn's text deltas (the flat
 *  `content` string). When it already equals the replacement, the streamed parts ARE
 *  canonical: keep them untouched, preserving the text↔tool interleaving. Only on a
 *  real divergence (frames lost/duplicated en route) do we rebuild — drop every prior
 *  text run and land the canonical text as one trailing run. That trades the (already
 *  unreliable) interleaving for the guarantee the answer renders exactly once. */
export function replaceText(parts: ChatPart[] | undefined, text: string, streamed: string): ChatPart[] {
  const next = [...(parts ?? [])];
  if (streamed.trim() === text.trim()) return next;
  const kept = next.filter((p) => p.kind !== "text");
  const trimmed = text.replace(/^\s+/, "");
  if (!trimmed) return kept;
  return [...kept, { kind: "text", text: trimmed }];
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

/** Split a streamed text delta into word-granular reveal chunks (revealQueue.ts, #2993):
 *  each chunk is a whitespace run glued to the word it precedes, and a pure-whitespace
 *  tail rides as its own chunk — so `chunks.join("") === text` exactly, and the
 *  reveal-paced appendText path accumulates byte-identical content to the instant path.
 *  Pacing changes WHEN text lands, never what lands. A word split across two SSE deltas
 *  reveals as two chunks; they concatenate in the open run, so nothing is held back. */
export function splitRevealChunks(text: string): string[] {
  return text.match(/\s*\S+|\s+$/g) ?? (text ? [text] : []);
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
  if (!fold) return { fold, workParts: baseWork, answerParts: parts.slice(split) };
  // A folded turn NEVER hides a component. `show_component` is a render directive for the
  // user — "renders immediately" is its contract — and a reasoning model thinks between the
  // component and its final text (…component → reasoning → text), so the trailing-run walk
  // above stops at the reasoning part and the component would be stranded inside the
  // collapsed "Worked" disclosure: the tool ran, the server emitted it, and nothing visible
  // happened (#2965 — protoEngineer on opus: three show_component calls, nothing rendered).
  // So components are lifted out of the work timeline and lead the answer, in emission
  // order — while streaming too: a component can't "flash then jump back" the way interim
  // narration does (the settle guard's reason), because it is always promoted.
  const isComponent = (p: ChatPart) => p.kind === "component";
  if (streaming) return { fold, workParts: parts.filter((p) => !isComponent(p)), answerParts: parts.filter(isComponent) };
  return {
    fold,
    workParts: baseWork.filter((p) => !isComponent(p)),
    answerParts: [...baseWork.filter(isComponent), ...parts.slice(split)],
  };
}

/** The tool calls to render for a `tools` part: its top-level calls (by id) plus any
 *  subagent children nested under them — so ToolCalls can rebuild the nesting. */
export function toolsForGroup(ids: string[], calls: ToolCall[] | undefined): ToolCall[] {
  const set = new Set(ids);
  return (calls ?? []).filter((c) => set.has(c.id) || (c.parentId != null && set.has(c.parentId)));
}

/** The id of the LAST user/assistant message — the conversational tail. "Rewind to
 *  here" discards everything BELOW a message, so on the tail it is a guaranteed
 *  no-op behind a destructive confirmation; the action row hides it there. Local
 *  system notes don't count (display-only, never in the checkpoint) — a trailing
 *  errored USER bubble does, which keeps Rewind on the assistant message before it
 *  (discarding the stranded user message is exactly what Rewind is for). */
export function rewindableTailId(messages: { id?: string; role: string }[]): string | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if ((m.role === "user" || m.role === "assistant") && m.id) return m.id;
  }
  return undefined;
}
