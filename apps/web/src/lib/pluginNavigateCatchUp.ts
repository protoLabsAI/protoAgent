// Navigate catch-up for plugin views (#4283, ADR 0044 × ADR 0039).
//
// A plugin tool commonly does "publish, then navigate": Atlas's `atlas_show` emits
// `atlas.show` and then `registry.navigate("atlas")`. When the view is already mounted the
// navigate changes nothing (the iframe stays, the event arrives live). But when it ISN'T
// mounted (another surface took its dock: `show_code` swapping the right dock to the Code
// pane, the operator clicking elsewhere, a collapsed dock), the navigate MOUNTS a fresh page.
// That page subscribes after the event was published, and a page that subscribes without a
// `since` never sees it. Its first subscribe is too late by construction.
//
// So a navigate to an UNMOUNTED view arms a one-shot catch-up: the fresh page's first
// subscribe is answered as if it had asked for `since`, and gets this plugin's retained
// events from just before the navigate. The lower bound is the later of:
//   • the previous instance's high-water mark (the bus seq when it unmounted). Whatever it
//     saw is not re-delivered, and
//   • a short window before the navigate, so a view never mounted this session does not get
//     a backlog of old events, only what the navigating tool just published.
// A page that sends its own `since` stays authoritative (it already gets the replay).
//
// Nothing here makes the console remount anything. Mount tracking is only the "is it
// already open?" check, and an open view keeps its iframe.
import { latestSeq, seqReceivedBefore } from "./events";

/** How far before a navigate its catch-up reaches: the tool's own publishes land
 *  milliseconds before the navigate, so this is generous, not tight. */
export const NAVIGATE_CATCH_UP_WINDOW_MS = 10_000;
/** How long an armed catch-up waits for the page's first subscribe (a cold plugin page
 *  can take a few seconds to load); after that it is dropped unused. */
export const NAVIGATE_CATCH_UP_TTL_MS = 30_000;

type Deps = {
  latestSeq: () => number | null;
  seqReceivedBefore: (cutoffMs: number) => number | null;
  now: () => number;
};
const defaultDeps: Deps = { latestSeq, seqReceivedBefore, now: () => Date.now() };

// Live (non-embedded) PluginView mounts per view key. A count, not a flag: StrictMode and
// a key change can overlap a mount with the previous one's cleanup.
const mounted = new Map<string, number>();
// The bus seq when each view last unmounted, i.e. what its previous page had been offered.
const lastSeen = new Map<string, number>();
const armed = new Map<string, { since: number; expires: number }>();

/** Record that a live PluginView for `viewKey` is mounted. Returns the unmount callback,
 *  which stamps the view's high-water mark. */
export function trackPluginViewMount(viewKey: string, deps: Pick<Deps, "latestSeq"> = defaultDeps): () => void {
  mounted.set(viewKey, (mounted.get(viewKey) ?? 0) + 1);
  return () => {
    const n = (mounted.get(viewKey) ?? 1) - 1;
    if (n > 0) mounted.set(viewKey, n);
    else mounted.delete(viewKey);
    const seq = deps.latestSeq();
    if (seq !== null) lastSeen.set(viewKey, seq);
  };
}

export function isPluginViewMounted(viewKey: string): boolean {
  return (mounted.get(viewKey) ?? 0) > 0;
}

/** A `ui.navigate` is about to open `viewKey`. If its page isn't mounted, arm the catch-up
 *  for the page that is about to be. Returns whether one was armed (an already-open view
 *  needs none, since its live relay already delivered everything). */
export function armNavigateCatchUp(viewKey: string, deps: Deps = defaultDeps): boolean {
  if (isPluginViewMounted(viewKey)) return false;
  const now = deps.now();
  const windowFloor = deps.seqReceivedBefore(now - NAVIGATE_CATCH_UP_WINDOW_MS) ?? -1;
  const since = Math.max(windowFloor, lastSeen.get(viewKey) ?? -1);
  armed.set(viewKey, { since, expires: now + NAVIGATE_CATCH_UP_TTL_MS });
  return true;
}

/** Take (and clear) the armed catch-up for `viewKey`: the `since` to answer the page's
 *  first subscribe with, or null when none is armed or it expired. */
export function takeNavigateCatchUp(viewKey: string, deps: Pick<Deps, "now"> = defaultDeps): number | null {
  const a = armed.get(viewKey);
  if (!a) return null;
  armed.delete(viewKey);
  return deps.now() <= a.expires ? a.since : null;
}

/** Test seam: forget all mount/arm state. */
export function resetNavigateCatchUp(): void {
  mounted.clear();
  lastSeen.clear();
  armed.clear();
}
