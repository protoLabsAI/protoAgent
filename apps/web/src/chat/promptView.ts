// Pure helpers behind the "View prompt" dialog + /prompt note (#2243) — kept
// component-free so the tab mapping, usage line, and note markdown are unit
// testable (the vitest harness is .test.ts only).

import type { PromptBreakdown, PromptCall, PromptSection } from "../lib/types";

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

// ── #2388 P3: section-level diff ─────────────────────────────────────────────

/** One section-level change vs the comparison call. `delta` is chars (cur − prev). */
export type SectionDelta = {
  label: string;
  kind: "added" | "removed" | "resized" | "relabeled";
  delta: number;
};

/** Labels carry live counts ("Injected memory (2 sessions · 3 memories)") — match
 *  sections across calls on the base name, so a count change reads as the SAME
 *  section changing size, not a vanish+appear pair. */
export const baseLabel = (l: string): string => l.replace(/\s*\(.*\)\s*$/, "");

/** Section-level diff (#2388 P3): what appeared, vanished, or changed size between
 *  two captured calls — computed from the P2 section rows, no text diffing. */
export function sectionDiff(
  prev: PromptSection[] | undefined | null,
  cur: PromptSection[] | undefined | null,
): SectionDelta[] {
  const p = prev ?? [];
  const c = cur ?? [];
  const pby = new Map<string, PromptSection>();
  for (const s of p) if (!pby.has(baseLabel(s.label))) pby.set(baseLabel(s.label), s);
  const seen = new Set<string>();
  const out: SectionDelta[] = [];
  for (const s of c) {
    const key = baseLabel(s.label);
    seen.add(key);
    const was = pby.get(key);
    if (!was) {
      out.push({ label: s.label, kind: "added", delta: s.chars });
    } else if (was.chars !== s.chars) {
      out.push({ label: s.label, kind: "resized", delta: s.chars - was.chars });
    } else if (was.label !== s.label) {
      // Same size, new count-carrying label ("2 memories" → "3 memories · 1 docs"
      // can keep chars equal only by coincidence, but reordered counts can't hide).
      out.push({ label: s.label, kind: "relabeled", delta: 0 });
    }
  }
  for (const s of p) {
    if (!seen.has(baseLabel(s.label))) out.push({ label: s.label, kind: "removed", delta: -s.chars });
  }
  return out;
}

/** The one-line diff summary for the meta strip. `deltas === null` = no comparison
 *  target (first turn, incognito gap, retention trim, unsegmented capture) — say so
 *  honestly instead of pretending "unchanged". */
export function diffLine(deltas: SectionDelta[] | null, anchor: string): string {
  if (deltas === null) return "no comparison available";
  if (!deltas.length) return `unchanged vs ${anchor}`;
  const bits = deltas.slice(0, 4).map((d) =>
    d.kind === "added"
      ? `+ ${d.label}`
      : d.kind === "removed"
        ? `− ${baseLabel(d.label)}`
        : d.kind === "relabeled"
          ? `${d.label}`
          : `${baseLabel(d.label)} ${d.delta > 0 ? "+" : "−"}${fmtTok(Math.abs(d.delta))} chars`,
  );
  const more = deltas.length > 4 ? ` · ${deltas.length - 4} more` : "";
  return `vs ${anchor}: ${bits.join(" · ")}${more}`;
}

// ── #2843: conversation-history breakdown (the context audit, console-side) ──


/** Human labels for the audit's category keys — anything unknown title-cases. */
const CATEGORY_LABELS: Record<string, string> = {
  tool_call_args: "Tool call args",
  tool_results: "Tool results",
  injected_context_frames: "Injected memory frames",
  assistant_text: "Assistant text",
  operator_messages: "Operator messages",
};

export const categoryLabel = (key: string): string =>
  CATEGORY_LABELS[key] ?? key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

/** Budget-row shape for the history block — same proportional-bar contract as
 *  budgetRows, so the dialog renders both groups identically. */
export function historyRows(b: PromptBreakdown): BudgetRow[] {
  const entries = Object.entries(b.categories).filter(([, tok]) => tok > 0);
  const total = entries.reduce((n, [, tok]) => n + tok, 0);
  if (!total) return [];
  return entries.map(([key, tok]) => ({
    label: categoryLabel(key),
    chars: tok * 4, // the audit reports ≈tokens (chars//4); bars use chars like budgetRows
    approx_tokens: tok,
    scope: "context",
    pct: Math.max(1, Math.round((tok / total) * 100)),
  }));
}

/** "top: plugin_write_file 22.6k · develop_plugin 9.1k · write_note 1.5k" — the
 *  three biggest arg producers; "" when the thread has no tool calls. */
export function historyTopToolsLine(b: PromptBreakdown, n = 3): string {
  const top = Object.entries(b.tool_call_args).slice(0, n);
  if (!top.length) return "";
  return `top: ${top.map(([name, tok]) => `${name} ${fmtTok(tok)}`).join(" · ")}`;
}

/** The /prompt note's history line: total + categories + top arg producers. */
export function historyLine(b: PromptBreakdown): string {
  const cats = Object.entries(b.categories)
    .filter(([, tok]) => tok > 0)
    .map(([key, tok]) => `${categoryLabel(key).toLowerCase()} ${fmtTok(tok)}`)
    .join(" · ");
  const top = historyTopToolsLine(b);
  return `_History (≈tokens):_ ${fmtTok(b.total_est_tokens)} across ${b.message_count} messages — ${cats}${top ? ` — ${top}` : ""}`;
}
