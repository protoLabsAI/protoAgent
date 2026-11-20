// The pinned Home tab (H-U1) is a real chat session, but the tab strip always shows it FIRST
// regardless of where it sits in the chat store's session list (`ensureSession` appends it).
// Everything that walks "the tabs in order" outside ChatSurface — the ⌘1–9 / Ctrl+Tab
// keybindings, the mobile session sheet — must use the same order, so the helper lives here.
import { currentSlug } from "../lib/api";
import { queryClient } from "../lib/queryClient";

/** Query key of `GET /api/home` — shared by ChatSurface's query and the readers below. */
export const homeQueryKey = () => [currentSlug(), "home"] as const;

/** The Home session id from the already-loaded `GET /api/home` state; undefined when the
 *  feature is off (404 → null), not loaded yet, or failed. Never fetches. */
export function homeSessionId(): string | undefined {
  const data = queryClient.getQueryData<{ session_id?: string } | null>(homeQueryKey());
  return data?.session_id || undefined;
}

/** `sessions` in tab-strip order: Home first (when it is one of them), the rest unchanged. */
export function inTabOrder<T extends { id: string }>(sessions: readonly T[], homeId = homeSessionId()): T[] {
  const home = homeId ? sessions.find((session) => session.id === homeId) : undefined;
  return home ? [home, ...sessions.filter((session) => session !== home)] : [...sessions];
}
