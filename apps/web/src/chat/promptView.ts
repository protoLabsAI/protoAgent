// Pure helpers behind the "View prompt" dialog + /prompt note (#2243) — kept
// component-free so the tab mapping, usage line, and note markdown are unit
// testable (the vitest harness is .test.ts only).

import type { PromptBreakdown, PromptCall, PromptRetention, PromptSection } from "../lib/types";

/** How much prompt text the /prompt system note shows before deferring to the
 *  full viewer — a note is a glance, not a reading surface. */
export const PROMPT_NOTE_CAP = 6000;

/** Compact token count — the UsageFooter convention (12345 → "12.3k"). */
export function fmtTok(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** Byte-for-byte what the model received: stable prefix, then the per-turn
 *  projection, then the legacy volatile tail — the same order `_sections`
 *  reports (stable → projected → context).
 *
 *  Post-#3234 captures and the /preview synthesis carry an EMPTY
 *  `system.context` and put everything in `projected_context`, so reading only
 *  stable+context renders an empty body for exactly the calls this dialog is
 *  now most used to inspect. Pre-#3188 rows are the mirror case (context set,
 *  projected empty), and concatenating both is safe because the server never
 *  populates the two channels for the same call. */
export function promptText(call: PromptCall): string {
  return `${call.system.stable}${call.projected_context ?? ""}${call.system.context}`;
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

/** "stable 41.2k chars · context tail 1.3k chars" — where the split lands. The
 *  tail counts BOTH channels: post-#3234 calls carry the projection in
 *  `projected_context` and leave `system.context` empty, and reporting 0 there
 *  read as "nothing dynamic was delivered" when several kB had been. */
export function splitLine(call: PromptCall): string {
  const tail = (call.projected_context ?? "").length + call.system.context.length;
  return `stable ${fmtTok(call.system.stable.length)} chars · context tail ${fmtTok(tail)} chars`;
}

/** The delivery budget as a renderable summary (ADR 0108 D6) — the ceiling, what
 *  was delivered against it, the headroom left, and a fill percent for the bar.
 *  `null` when the call composed unbounded (no budget in force), which is the
 *  common case on a large-window model.
 *
 *  Distinct from `budgetRows` below: this is the DELIVERY CEILING, that is the
 *  section-size breakdown. They answer different questions and must not be
 *  labeled alike. */
export type DeliveryBudget = {
  chars: number;
  used: number;
  headroom: number;
  pct: number;
  overflow: { label: string; dropped_items: number; dropped_chars: number }[];
};

export function deliveryBudget(call: PromptCall): DeliveryBudget | null {
  const b = call.budget;
  if (!b || !b.chars) return null;
  const used = Math.max(0, b.used);
  return {
    chars: b.chars,
    used,
    headroom: Math.max(0, b.chars - used),
    // Clamped: an over-budget compose (working state + always-on alone
    // exceeding the ceiling, which D6 delivers rather than sheds) must render
    // as a full bar, not overflow the track.
    pct: Math.min(100, Math.max(1, Math.round((used / b.chars) * 100))),
    overflow: b.overflow ?? [],
  };
}

/** "Prior sessions −3 entries (−1.2k chars) · RAG hits −8 entries (−9.1k chars)"
 *  — what the budget dropped, in shed order. Empty when nothing was shed. */
export function overflowLine(call: PromptCall): string {
  const rows = call.budget?.overflow ?? [];
  if (!rows.length) return "";
  return rows
    .map((o) => `${o.label} −${o.dropped_items} ${o.dropped_items === 1 ? "entry" : "entries"} (−${fmtTok(o.dropped_chars)} chars)`)
    .join(" · ");
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

/** The /prompt note's retention line — "" unless the server says the ROW cap is
 *  what ends the window (#3019).
 *
 *  "Nothing captured for this session yet" is the wrong story when the store is
 *  full: the captures existed and the row cap threw them away, and before this
 *  the only way to learn that was to open prompt-snapshots.db. Deliberately does
 *  NOT claim this session HAD captures — the store reports its own window, not
 *  per-session history — and stays silent on an older server, which sends no
 *  `retention` block at all. */
export function retentionLine(r?: PromptRetention): string {
  if (!r || r.binding_cap !== "max_calls") return "";
  const span = r.effective_days == null ? "" : `, ~${r.effective_days}d held`;
  return (
    `_Retention:_ the snapshot store is full at its row cap (\`prompts.max_calls\` = ${r.max_calls}${span}), ` +
    `so older captures are already evicted no matter what \`prompts.retention_days\` is set to — ` +
    `raise the cap in Settings ▸ Telemetry to keep a longer window.`
  );
}
