// Local-only dismissal of FINISHED goals from the live surfaces — the Work overview card's
// "Recent" list and a chat tab's goal strip. A finished goal stays visible there for
// RECENT_GOAL_WINDOW_S so the operator sees it land; dismissing hides it sooner. The Goals
// panel (the full list) is unaffected, and nothing on the server changes.
//
// Keyed by `goalDismissKey` (session + finish time), so a goal that is restarted and finishes
// again reappears. One shared store, so dismissing on the card also hides the chat strip.
// localStorage is a per-viewer convenience here: every read/write is guarded, and a blocked
// store just means dismissals last until reload.

import { useSyncExternalStore } from "react";

const KEY = "protoagent.goals.dismissed";
const CAP = 100;
const listeners = new Set<() => void>();

function load(): Set<string> {
  try {
    return new Set(JSON.parse(window.localStorage.getItem(KEY) || "[]"));
  } catch {
    return new Set();
  }
}

let snapshot: Set<string> = load();

/** Hide a finished goal (by its `goalDismissKey`) from the card + chat strip. */
export function dismissGoal(key: string) {
  const next = new Set(snapshot);
  next.add(key);
  snapshot = next;
  try {
    window.localStorage.setItem(KEY, JSON.stringify([...next].slice(-CAP)));
  } catch {
    // Storage blocked (private window / preview) — the dismissal holds until reload.
  }
  listeners.forEach((l) => l());
}

/** Test hook — forget every dismissal. */
export function resetDismissedGoals() {
  snapshot = new Set();
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // ignore
  }
  listeners.forEach((l) => l());
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** The current dismissed set (a stable object until the next dismissal). */
export function useDismissedGoals(): ReadonlySet<string> {
  return useSyncExternalStore(subscribe, () => snapshot, () => snapshot);
}
