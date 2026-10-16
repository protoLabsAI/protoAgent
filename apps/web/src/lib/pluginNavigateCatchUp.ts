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
// Nothing here makes the console remount anything. Tracking is only the "is it already
// open and listening?" check, and an open view keeps its iframe.
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

// Live (non-embedded) PluginView pages per view key that have SUBSCRIBED, i.e. whose relay
// is actually delivering. Mounted-but-not-yet-subscribed doesn't count: a show published
// while the iframe is still probing/loading reaches no one live, so a navigate then still
// needs the catch-up. A count, not a flag: StrictMode and a key change can overlap a mount
// with the previous one's cleanup.
const live = new Map<string, number>();
// The bus seq when each view's last SUBSCRIBED page went away, i.e. what it had been offered.
// A page that unmounts before subscribing received nothing, so it leaves the mark alone.
const lastSeen = new Map<string, number>();
const armed = new Map<string, { since: number; expires: number }>();

export type PluginViewMountTracker = {
  /** The page subscribed: from now on it receives this view's events live. */
  markSubscribed: () => void;
  /** The page is going away (unmount / re-pointed frame). */
  release: () => void;
};

/** Track one live PluginView page for `viewKey`. */
export function trackPluginViewMount(
  viewKey: string,
  deps: Pick<Deps, "latestSeq"> = defaultDeps,
): PluginViewMountTracker {
  let subscribed = false;
  let released = false;
  return {
    markSubscribed() {
      if (subscribed || released) return;
      subscribed = true;
      live.set(viewKey, (live.get(viewKey) ?? 0) + 1);
    },
    release() {
      if (released) return;
      released = true;
      if (!subscribed) return;
      const n = (live.get(viewKey) ?? 1) - 1;
      if (n > 0) live.set(viewKey, n);
      else live.delete(viewKey);
      const seq = deps.latestSeq();
      if (seq !== null) lastSeen.set(viewKey, seq);
    },
  };
}

/** Is a subscribed page for `viewKey` receiving its events live right now? */
export function isPluginViewLive(viewKey: string): boolean {
  return (live.get(viewKey) ?? 0) > 0;
}

/** A `ui.navigate` is about to open `viewKey`. If its page isn't mounted
 *  (or not yet subscribed), arm the catch-up for its first subscribe. Returns whether one
 *  was armed (a live view needs none, since its relay already delivered everything). */
export function armNavigateCatchUp(viewKey: string, deps: Deps = defaultDeps): boolean {
  if (isPluginViewLive(viewKey)) return false;
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
  live.clear();
  lastSeen.clear();
  armed.clear();
}
