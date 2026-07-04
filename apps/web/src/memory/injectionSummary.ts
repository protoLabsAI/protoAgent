// Plain-language "what it used" summary for one injection record — the legible
// replacement for the old forensics columns of comma-joined raw ids. Built
// purely from the id-array LENGTHS the list route already returns, so the table
// needs no backend resolve; the detail dialog fetches the actual items on click.
//
// The forensic id groups map to friendly names (ADR 0069 D6):
//   hot_chunk_ids      → "memories"     (always-on hot memory)
//   digest_session_ids → "past chats"   (prior-session digest)
//   rag_chunk_ids      → "docs"         (knowledge retrieval)
// Empty groups are dropped; when the turn injected nothing extra we show a dash.

// Only the length of each id group matters here, so accept the widest shape.
type InjectionCounts = {
  digest_session_ids: readonly unknown[];
  hot_chunk_ids: readonly unknown[];
  rag_chunk_ids: readonly unknown[];
};

// The em dash the console uses elsewhere for an empty memory cell (.memory-none).
export const NOTHING_USED = "—";

function count(n: number, singular: string, plural: string): string {
  return `${n} ${n === 1 ? singular : plural}`;
}

// e.g. `3 memories · 2 past chats · 4 docs`. Groups appear memories → past chats
// → docs, matching the detail dialog's order; a group with zero items is omitted.
// All three empty → `NOTHING_USED`.
export function injectionSummary(row: InjectionCounts): string {
  const parts: string[] = [];
  if (row.hot_chunk_ids.length) parts.push(count(row.hot_chunk_ids.length, "memory", "memories"));
  if (row.digest_session_ids.length)
    parts.push(count(row.digest_session_ids.length, "past chat", "past chats"));
  if (row.rag_chunk_ids.length) parts.push(count(row.rag_chunk_ids.length, "doc", "docs"));
  return parts.length ? parts.join(" · ") : NOTHING_USED;
}
