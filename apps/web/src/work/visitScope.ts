/** The "this chat" scope a chat-rail deep link carries into Work (work consolidation v2:
 *  "Open in Work — deep link to that group, `thisChat`"). The scope rides OUTSIDE the saved
 *  surface state: the deep link scopes the visit it opens, while Work's saved filters — what
 *  every other entry (the dock, the palette, /directives) restores — never change. Work itself
 *  always lists every chat's work and has no scope toggle (Josh 2026-11-11), so a persisted
 *  flag would be irreversible; this one ends with the visit instead: when the reader picks a
 *  section themselves, enters Work through another path, or the last mounted Work view leaves
 *  the screen. */

type Listener = () => void;

let thisChatVisit = false;
const listeners = new Set<Listener>();

function emit() {
  for (const listener of listeners) listener();
}

/** A rail deep link is opening Work scoped to the chat it counted. */
export function beginThisChatVisit() {
  if (thisChatVisit) return;
  thisChatVisit = true;
  emit();
}

/** The visit ended — or an unscoped entry superseded it. Back to the saved filters. */
export function endThisChatVisit() {
  if (!thisChatVisit) return;
  thisChatVisit = false;
  emit();
}

export function isThisChatVisit(): boolean {
  return thisChatVisit;
}

export function subscribeThisChatVisit(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

// ── Visit lifetime ────────────────────────────────────────────────────────────────────
// "Leaving Work" is the last mounted Work view going away. The end is deferred one tick
// because a Work view can remount without the visit ending: StrictMode's dev
// mount → cleanup → mount, and the Expand / Dock frame swap — the surviving (or next)
// mount cancels the pending end before it runs.
let mounts = 0;
let pendingEnd: ReturnType<typeof setTimeout> | null = null;

export function trackWorkViewMount(): () => void {
  mounts += 1;
  if (pendingEnd !== null) {
    clearTimeout(pendingEnd);
    pendingEnd = null;
  }
  return () => {
    mounts -= 1;
    if (mounts > 0) return;
    pendingEnd = setTimeout(() => {
      pendingEnd = null;
      endThisChatVisit();
    }, 0);
  };
}
