/**
 * Display-casing for the agent's name.
 *
 * The backend treats the agent name as a slug — metrics lower-case + sanitize
 * it, the API-key env upper-cases it, data paths key off PROTOAGENT_INSTANCE —
 * so a fork commonly configures a lower-case identity (`gina`, `roxy`, `quinn`).
 * That slug looks wrong as a UI brand / browser-tab title. `brandName` renders
 * it for display: the protoAgent default keeps its camelCase brand, an
 * intentionally-cased name is respected as-is, and a bare lower-case slug gets a
 * capitalized first letter (`gina` → `Gina`). One helper, used everywhere the
 * name is shown (tab title, topbar, boot gate, runtime panel).
 */
export function brandName(name?: string | null): string {
  const n = (name ?? "").trim();
  if (!n || n.toLowerCase() === "protoagent") return "protoAgent";
  // Already carries intentional casing (any upper-case char) — respect it.
  if (n !== n.toLowerCase()) return n;
  // Bare lower-case slug → capitalize the first letter for display.
  return n.charAt(0).toUpperCase() + n.slice(1);
}
