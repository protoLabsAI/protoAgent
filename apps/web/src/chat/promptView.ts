// Pure helpers behind the "View prompt" dialog + /prompt note (#2243) — kept
// component-free so the tab mapping, usage line, and note markdown are unit
// testable (the vitest harness is .test.ts only).

import type { PromptCall, PromptSection } from "../lib/types";

/** How much prompt text the /prompt system note shows before deferring to the
 *  full viewer — a note is a glance, not a reading surface. */
export const PROMPT_NOTE_CAP = 6000;

/** Compact token count — the UsageFooter convention (12345 → "12.3k"). */
export function fmtTok(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** Byte-for-byte what the model received: stable prefix + volatile tail. */
export function promptText(call: PromptCall): string {
  return `${call.system.stable}${call.system.context}`;
}

/** One segmented-Tabs item per model call of the turn. */
export function callTabs(calls: PromptCall[]): { id: string; label: string }[] {
  return calls.map((c) => ({ id: String(c.call_index), label: `Call ${c.call_index + 1}` }));
}

/** "in 12.3k (cache read 11.9k) · out 420" — zeros collapse away. */
export function usageLine(call: PromptCall): string {
  const u = call.usage;
  if (!u.input_tokens && !u.output_tokens) return "";
  const cache = u.cache_read_tokens ? ` (cache read ${fmtTok(u.cache_read_tokens)})` : "";
  return `in ${fmtTok(u.input_tokens)}${cache} · out ${fmtTok(u.output_tokens)}`;
}

/** "stable 41.2k chars · context tail 1.3k chars" — where the split lands. */
export function splitLine(call: PromptCall): string {
  const tail = call.system.context.length;
  return `stable ${fmtTok(call.system.stable.length)} chars · context tail ${fmtTok(tail)} chars`;
}

/** One renderable budget row (#2243 P2): the section plus its share of the
 *  whole prompt (percent of total section chars, for the proportional bar). */
export type BudgetRow = PromptSection & { pct: number };

/** The call's sections as budget rows — [] when captured unsegmented. */
export function budgetRows(call: PromptCall): BudgetRow[] {
  const sections = call.sections ?? [];
  const total = sections.reduce((n, s) => n + s.chars, 0);
  if (!total) return [];
  return sections.map((s) => ({ ...s, pct: Math.max(1, Math.round((s.chars / total) * 100)) }));
}

/** "SOUL 10.2k · Skills index 312 · Working state 19" — the one-line budget
 *  (approx tokens per section) for the /prompt note. Empty when unsegmented. */
export function sectionsLine(call: PromptCall): string {
  const sections = call.sections ?? [];
  if (!sections.length) return "";
  return sections.map((s) => `${s.label} ${fmtTok(s.approx_tokens)}`).join(" · ");
}

/** The /prompt system note: header + fenced prompt text, truncated at `cap`.
 *  Four-backtick fence so prompt bodies containing ``` don't break out. */
export function promptNoteMarkdown(call: PromptCall, cap: number = PROMPT_NOTE_CAP): string {
  const text = promptText(call);
  const clipped = text.length > cap;
  const shown = clipped ? text.slice(0, cap) : text;
  const usage = usageLine(call);
  const budget = sectionsLine(call);
  const header =
    `**System prompt** — last model call of this session _(not saved to the conversation)_\n\n` +
    `\`${call.model || "unknown model"}\` · ${splitLine(call)}${usage ? ` · ${usage}` : ""}` +
    (budget ? `\n\n_Budget (≈tokens):_ ${budget}` : "");
  const tail = clipped
    ? `\n\n_Showing ${fmtTok(cap)} of ${fmtTok(text.length)} chars — open **View prompt** on an assistant message for the full text._`
    : "";
  return `${header}\n\n\`\`\`\`text\n${shown}\n\`\`\`\`${tail}`;
}
