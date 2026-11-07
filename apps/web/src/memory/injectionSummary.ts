// Plain-language "what it used" summary for one injection record — the legible
// replacement for the old forensics columns of comma-joined raw ids. Built
// from the id arrays the list route already returns, so the table
// needs no backend resolve; the detail dialog fetches the actual items on click.
//
// The forensic id groups map to friendly names (ADR 0069 D6):
//   hot_chunk_ids      → "memories"     (always-on hot memory)
//   digest_session_ids → "past chats"   (prior-session digest)
//   rag_chunk_ids      → "memories" for memory:<id>, otherwise "docs"
// Empty groups are dropped; when the turn injected nothing extra we show a dash.

// Memory handles distinguish retrieved memories from historical chunk ids.
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
  const retrievedMemories = row.rag_chunk_ids.filter(id => typeof id === "string" && id.startsWith("memory:")).length;
  const memories = row.hot_chunk_ids.length + retrievedMemories;
  const docs = row.rag_chunk_ids.length - retrievedMemories;
  if (memories) parts.push(count(memories, "memory", "memories"));
  if (row.digest_session_ids.length)
    parts.push(count(row.digest_session_ids.length, "past chat", "past chats"));
  if (docs) parts.push(count(docs, "doc", "docs"));
  return parts.length ? parts.join(" · ") : NOTHING_USED;
}
