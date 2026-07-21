// Bulk chat-tab close (Close others / left / right, #context-menu). Pure index math over the
// ordered session list so it can be unit-tested without React or the store — ChatSurface owns
// the side effects (confirm dialog, deleteSession). Chat sessions have no "pinned" concept
// (unlike model favorites), so nothing needs excluding beyond the right-clicked anchor itself.

export type BulkCloseMode = "others" | "left" | "right";

/**
 * The session ids a bulk-close action targets, given the ordered session list and the anchor
 * (right-clicked) tab. Index-based against `sessions` order:
 *   - "others" → every tab except the anchor
 *   - "left"   → every tab positioned before the anchor
 *   - "right"  → every tab positioned after the anchor
 * The anchor is never included. An anchor that isn't in the list yields an empty array (nothing
 * to close), as does any mode with no tabs on the requested side (e.g. "left" on the first tab).
 */
export function sessionsToClose<T extends { id: string }>(
  sessions: readonly T[],
  anchorId: string,
  mode: BulkCloseMode,
): string[] {
  const anchor = sessions.findIndex((s) => s.id === anchorId);
  if (anchor < 0) return [];
  return sessions
    .filter((_, i) => {
      if (i === anchor) return false;
      if (mode === "left") return i < anchor;
      if (mode === "right") return i > anchor;
      return true; // "others"
    })
    .map((s) => s.id);
}
