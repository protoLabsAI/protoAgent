import { useSyncExternalStore } from "react";

import { decisionsApi } from "../lib/api/decisions";
import type { AnswerResult, DecisionList, DecisionRow } from "../lib/api/decisions";
import { onConnectionChange, onTopic } from "../lib/events";
import { notifyIfHidden } from "../lib/notify";

/**
 * The fleet-wide "decisions waiting on you" store (#4725, §7). ONE store, keyed by decision id
 * so the pill, the phone button, the panel, the source chat and the notification all read the
 * same truth with no duplicates. It loads once, refreshes on the `decision.changed` bus event
 * and on reconnect, and never polls — the `useDirectives` pattern (#4861), but singleton and
 * fleet-wide (the hub sees every member's events).
 *
 * It also owns the panel's open state (so a notification click or an Activity link can open it
 * from outside, §2.3/Q11) and the one-shot notification latch (notify once per new pending id,
 * never again on a reload, §4.5).
 */
export type DecisionsSnapshot = {
  state: "loading" | "ready" | "failed" | "stale";
  /** Pending decisions, in the server's order (soonest expiry, then undated oldest first). */
  decisions: DecisionRow[];
  pendingCount: number;
  /** The first pending decision (the pill's hover preview and the panel's top card), or null. */
  top: DecisionRow | null;
  /** False until the first list resolves — the entry points stay hidden so they never flash "0". */
  loaded: boolean;
  loadedAt: number | null;
  /** Whether the Decisions panel is open — a notification click or a link flips it on. */
  panelOpen: boolean;
};

/** Build a fresh store. Production uses one shared instance ({@link useDecisions}); tests build
 *  isolated ones so the notification latch and panel state never leak between cases. */
export function createDecisionsStore() {
  let snapshot: DecisionsSnapshot = {
    state: "loading",
    decisions: [],
    pendingCount: 0,
    top: null,
    loaded: false,
    loadedAt: null,
    panelOpen: false,
  };
  const listeners = new Set<() => void>();
  let stop: (() => void) | null = null;
  let pending: Promise<DecisionList | null> | null = null;
  let dirty = false;
  // One-shot notification latch: an id enters here the first time it appears pending and never
  // notifies again — not on a reconnect refetch, not on a decision.changed refresh (§4.5).
  const notified = new Set<string>();

  function emit() {
    for (const listener of listeners) listener();
  }

  // Stable identity so the notification's onclick and an Activity link both call the same
  // opener, and tests can assert the exact callback.
  function openPanel() {
    if (!snapshot.panelOpen) {
      snapshot = { ...snapshot, panelOpen: true };
      emit();
    }
  }
  function closePanel() {
    if (snapshot.panelOpen) {
      snapshot = { ...snapshot, panelOpen: false };
      emit();
    }
  }

  // Coalesce invalidations during a request, then fetch again: a response already in flight may
  // predate the event that just arrived (mirrors useDirectives).
  function refresh(): Promise<DecisionList | null> {
    dirty = true;
    if (pending) return pending;
    pending = (async () => {
      let result: DecisionList | null = null;
      do {
        dirty = false;
        try {
          result = await decisionsApi.listDecisions("pending");
          const decisions = result.decisions;
          snapshot = {
            ...snapshot,
            state: "ready",
            decisions,
            pendingCount: decisions.length,
            top: decisions[0] ?? null,
            loaded: true,
            loadedAt: Date.now(),
          };
          // Notify once per newly-seen pending id. notifyIfHidden itself no-ops when the window
          // is focused, so a reload while looking at the console stays quiet.
          for (const d of decisions) {
            if (notified.has(d.id)) continue;
            notified.add(d.id);
            notifyIfHidden(`${d.agent || "An agent"} needs a decision`, d.question, openPanel);
          }
        } catch {
          result = null;
          snapshot = { ...snapshot, state: snapshot.loaded ? "stale" : "failed" };
        }
        emit();
      } while (dirty);
      pending = null;
      return result;
    })();
    return pending;
  }

  async function answer(id: string, optionId: string): Promise<AnswerResult> {
    const result = await decisionsApi.answerDecision(id, { option_id: optionId, surface: "console" });
    // The server also publishes decision.changed, but refresh now so the count drops promptly.
    await refresh();
    return result;
  }

  async function cancel(id: string, reason?: string): Promise<DecisionRow> {
    const { decision } = await decisionsApi.cancelDecision(id, reason);
    await refresh();
    return decision;
  }

  function subscribe(listener: () => void) {
    listeners.add(listener);
    if (!stop) {
      void refresh();
      let initial = true;
      const offConn = onConnectionChange((connected) => {
        if (initial) {
          initial = false;
          return;
        }
        if (connected) void refresh();
      });
      // Fleet-wide: any member's decision.changed refreshes the one store.
      const offTopic = onTopic("decision.changed", () => void refresh());
      stop = () => {
        offConn();
        offTopic();
      };
    }
    return () => {
      listeners.delete(listener);
      if (!listeners.size) {
        stop?.();
        stop = null;
      }
    };
  }

  return { subscribe, getSnapshot: () => snapshot, refresh, answer, cancel, openPanel, closePanel };
}

// One store for the whole app: snapshots survive a consumer unmount/remount, and only while a
// consumer is subscribed does it hold live bus subscriptions.
const store = createDecisionsStore();

export function useDecisions() {
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  return {
    ...snapshot,
    refresh: store.refresh,
    answer: store.answer,
    cancel: store.cancel,
    openPanel: store.openPanel,
    closePanel: store.closePanel,
  };
}

/** Test seam: exercise the shared store + its bus subscriptions without React. */
export const decisionsStoreForTest = store;
