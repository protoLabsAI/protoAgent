// Ranking for the host-owned palette root view.
//
// UPSTREAM: protoLabsAI/protoContent#503 — @protolabsai/ui has no rank/sort seam. Its
// `commandsView` renders `groups.flatMap(g => g.commands)` in REGISTRATION order and its
// matcher is module-private with no exports-map entry, so a prefix match sorts below an
// incidental keyword hit and there is no hook to change that. When the DS ships
// `commandsView({ rank })`, delete this module's copy of `matchCommand` and hand `score`
// to the DS instead (see the note at the top of `rootView.tsx`).
//
// Two halves, deliberately separate, because conflating them is how a ranking layer turns
// into a silent regression:
//
//   • `matchCommand` decides INCLUSION and is a VERBATIM port of the DS matcher
//     (command-palette.views.tsx:48-56): split the query on whitespace, every term must be
//     a case-insensitive substring of `[label, hint, group, source.label, ...keywords]`
//     joined by spaces. Keyword-only matches are load-bearing — the Fleet Room command
//     carries every live member's name on its `keywords`, which is the only reason typing
//     "ava" finds it (e2e fleet.spec.ts:431-434).
//   • `rankCommands` decides ORDER, and ONLY order. It never drops a row `matchCommand`
//     kept, and it never caps.
import type { Command } from "@protolabsai/ui/command-palette";

/** Does this row match what the operator typed? A verbatim port of the DS commands view's
 *  module-private `matchCommand`. `rank.test.ts` pins these semantics precisely BECAUSE it
 *  is a copy: nothing but that test would notice the two drifting apart on a DS bump. */
export function matchCommand(c: Command, q: string): boolean {
  const query = q.trim().toLowerCase();
  if (!query) return true;
  const hay = [c.label, c.hint, c.group, c.source?.label, ...(c.keywords ?? [])]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return query.split(/\s+/).every((term) => hay.includes(term));
}

/** Match quality, best (lowest) first. 1-6 are the designed tiers; SPLIT is the residual
 *  bucket for a row the inclusion filter kept whose terms are spread across the label AND
 *  its metadata (e.g. "work goal" where "work" is the label and "goal" a keyword) — it has
 *  to exist because `matchCommand` joins every field into one haystack and so admits
 *  matches no single-field tier describes. */
export const TIER = {
  /** The label IS the query. */
  EXACT: 1,
  /** The label starts with the whole query. */
  PREFIX: 2,
  /** A word inside the label starts with one of the terms ("mem" → "Hot Memory"). */
  WORD_PREFIX: 3,
  /** Every term appears somewhere inside the label. */
  SUBSTRING: 4,
  /** Every term appears in the metadata — keywords / hint / group / source chip. This is
   *  the DS's existing semantics, and the floor a keyword-only row must never fall below. */
  META: 5,
  /** The query's characters appear in order in the label (fuzzy). */
  FUZZY: 6,
  /** Matched, but only by spreading terms across label and metadata. */
  SPLIT: 7,
} as const;

// Unicode-aware word split: "Settings: Fleet" → ["settings", "fleet"], "plugin:notes:x" →
// ["plugin", "notes", "x"], so a word-boundary prefix works on our id-ish labels too.
const WORD_SPLIT = /[^\p{L}\p{N}]+/u;

/** Are `needle`'s characters present in `hay`, in order? (Case-folded by the caller.) */
export function isSubsequence(needle: string, hay: string): boolean {
  if (!needle) return true;
  let i = 0;
  for (const ch of hay) {
    if (ch === needle[i]) i += 1;
    if (i === needle.length) return true;
  }
  return i === needle.length;
}

/** The tier a row lands in for `q`. Exported for the unit tests, which pin the ORDER of
 *  the tiers rather than the ordering of one particular corpus. */
export function tierFor(c: Command, q: string): number {
  const query = q.trim().toLowerCase();
  const label = (c.label ?? "").toLowerCase();
  if (!query) return TIER.EXACT;
  const terms = query.split(/\s+/);
  if (label === query) return TIER.EXACT;
  if (label.startsWith(query)) return TIER.PREFIX;
  const words = label.split(WORD_SPLIT).filter(Boolean);
  if (terms.some((t) => words.some((w) => w.startsWith(t)))) return TIER.WORD_PREFIX;
  if (terms.every((t) => label.includes(t))) return TIER.SUBSTRING;
  const meta = [c.hint, c.group, c.source?.label, ...(c.keywords ?? [])]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  if (terms.every((t) => meta.includes(t))) return TIER.META;
  if (isSubsequence(query.replace(/\s+/g, ""), label)) return TIER.FUZZY;
  return TIER.SPLIT;
}

export type RankOptions = {
  /** Frecency for a command id — higher sorts earlier. Applied WITHIN a tier only: a
   *  command you ran yesterday should not outrank the thing you are literally typing the
   *  name of, and a cross-tier boost would make "prefix beats keyword" depend on history
   *  (untestable, and surprising when two operators see different orders). */
  score?: (id: string) => number;
};

/** Order the rows `matchCommand` admits: tier, then frecency, then a stable tiebreak on
 *  the caller's order (which is registration order — the DS's own, so an unranked corpus
 *  comes out exactly as the DS would have rendered it). Returns a NEW array; the input is
 *  never mutated, and the result is never shorter than the matching subset. */
export function rankCommands(commands: Command[], q: string, opts: RankOptions = {}): Command[] {
  const query = q.trim();
  // The empty query is a DIFFERENT list (recents + a curated root, built by the view), so
  // there is nothing to rank — hand the corpus back untouched rather than inventing an order.
  if (!query) return [...commands];
  const score = opts.score ?? (() => 0);
  return commands
    .map((c, i) => ({ c, i }))
    .filter(({ c }) => matchCommand(c, query))
    .map((x) => ({ ...x, tier: tierFor(x.c, query), f: score(x.c.id) }))
    .sort((a, b) => a.tier - b.tier || b.f - a.f || a.i - b.i)
    .map((x) => x.c);
}
