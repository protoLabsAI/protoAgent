import { useEffect, useSyncExternalStore } from "react";

import { decisionsApi } from "../lib/api/decisions";
import type { AnswerResult, DecisionList, DecisionRow } from "../lib/api/decisions";
import { ApiError } from "../lib/api/http";
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
 *
 * Beyond the pending list it keeps a by-id record cache ({@link DecisionsSnapshot.records}) so a
 * chat transcript can read a decision that has since been answered, expired or withdrawn — the
 * row that left the pending list keeps its last known record until a fresh read replaces it
 * (#4725, Gap 2). Settled records live only in the cache: they never count as pending, never
 * notify and never show in the panel.
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
  /** Every decision we've seen, by id. Pending refreshes merge their rows in; answer/cancel and
   *  {@link loadDecision} store settled rows. A settled row stays here (and nowhere else) until a
   *  fresh read replaces it — the shared record a source chat reads. */
  records: Record<string, DecisionRow>;
  /** Ids a GET resolved as 404 — a caller renders "no longer available" without re-fetching. */
  missing: Record<string, true>;
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
    records: {},
    missing: {},
  };
  const listeners = new Set<() => void>();
  let stop: (() => void) | null = null;
  let pending: Promise<DecisionList | null> | null = null;
  let dirty = false;
  // One-shot notification latch: an id enters here the first time it appears pending and never
  // notifies again — not on a reconnect refetch, not on a decision.changed refresh (§4.5).
  const notified = new Set<string>();
  // In-flight GETs by id, so concurrent loadDecision callers share one request (and a
  // decision.changed re-read coalesces with a pending load).
  const inflight = new Map<string, Promise<DecisionRow | null>>();

  function emit() {
    for (const listener of listeners) listener();
  }

  // Store one record by id and drop any stale "missing" flag. Settled rows land here via
  // answer/cancel/loadDecision; pending rows are merged in bulk by refresh().
  function storeRecord(row: DecisionRow) {
    const records = { ...snapshot.records, [row.id]: row };
    let missing = snapshot.missing;
    if (missing[row.id]) {
      missing = { ...missing };
      delete missing[row.id];
    }
    snapshot = { ...snapshot, records, missing };
    emit();
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
          // Reconcile pending records that disappeared, including events missed while offline.
          // Resolve them before publishing the new list so selected details keep their identity
          // while transitioning to the terminal record. Failed reads can retry on the next refresh.
          const pendingIds = new Set(decisions.map((d) => d.id));
          await Promise.all(Object.values(snapshot.records)
            .filter((row) => row.state === "pending" && !pendingIds.has(row.id))
            .map((row) => fetchDecision(row.id)));
          const records = { ...snapshot.records };
          for (const d of decisions) records[d.id] = d;
          snapshot = {
            ...snapshot,
            state: "ready",
            decisions,
            pendingCount: decisions.length,
            top: decisions[0] ?? null,
            loaded: true,
            loadedAt: Date.now(),
            records,
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
    // Keep the now-closed record so the source chat can read it after it leaves the pending list.
    // Both result shapes carry `decision` (a 409 may carry null when the record is unavailable).
    if (result.decision) storeRecord(result.decision);
    // The server also publishes decision.changed, but refresh now so the count drops promptly.
    await refresh();
    return result;
  }

  async function cancel(id: string, reason?: string): Promise<DecisionRow> {
    const { decision } = await decisionsApi.cancelDecision(id, reason);
    storeRecord(decision);
    await refresh();
    return decision;
  }

  // GET one decision and store it (or latch a 404 as missing). Coalesces concurrent calls for
  // the same id; used both by loadDecision and the decision.changed re-read.
  function fetchDecision(id: string): Promise<DecisionRow | null> {
    const existing = inflight.get(id);
    if (existing) return existing;
    const p = (async () => {
      try {
        const row = await decisionsApi.getDecision(id);
        storeRecord(row);
        return row;
      } catch (err) {
        // A 404 means the row is gone for good — record it as missing (and drop any stale cached
        // row, e.g. a pending one being re-read) so callers render "no longer available" instead
        // of retrying in a loop. Any other error is transient and leaves the cache untouched.
        if (err instanceof ApiError && err.status === 404) {
          const records = { ...snapshot.records };
          delete records[id];
          snapshot = { ...snapshot, records, missing: { ...snapshot.missing, [id]: true } };
          emit();
        }
        return null;
      } finally {
        inflight.delete(id);
      }
    })();
    inflight.set(id, p);
    return p;
  }

  /** Load a decision by id on demand — a chat showing a decision that already left the pending
   *  list. A SETTLED cached record (answered/expired/cancelled/withdrawn) is terminal, so it
   *  resolves from the cache with no request; a known 404 resolves null the same way. A record
   *  still cached as PENDING is NOT trusted: it can be answered, expired or withdrawn elsewhere,
   *  and while no consumer is subscribed the store receives no `decision.changed` events — so it
   *  is re-read by GET (coalesced with any concurrent load) rather than served stale (§7,
   *  #4725 Gap 2). */
  function loadDecision(id: string): Promise<DecisionRow | null> {
    const cached = snapshot.records[id];
    if (cached && cached.state !== "pending") return Promise.resolve(cached);
    if (!cached && snapshot.missing[id]) return Promise.resolve(null);
    return fetchDecision(id);
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
      // Fleet-wide: any member's decision.changed refreshes the one store. The refresh covers
      // current pending rows and reconciles departed pending records. Also re-read a cached
      // terminal record named by the event, unless the refresh already replaced it (§7).
      const offTopic = onTopic("decision.changed", (payload) => {
        const id = typeof payload?.id === "string" ? payload.id : null;
        const cached = id != null ? snapshot.records[id] : undefined;
        void refresh().then(() => {
          if (id == null || !cached) return;
          if (snapshot.decisions.some((d) => d.id === id)) return;
          // refresh already reconciled departed pending records; don't read them twice.
          if (snapshot.records[id] !== cached) return;
          void fetchDecision(id);
        });
      });
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

  return { subscribe, getSnapshot: () => snapshot, refresh, answer, cancel, loadDecision, openPanel, closePanel };
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
    loadDecision: store.loadDecision,
    openPanel: store.openPanel,
    closePanel: store.closePanel,
  };
}

/** The resolution of one decision looked up by id: "loading" until the record arrives, "ready"
 *  once it is cached (pending or settled), "missing" once a GET returned 404. */
export type DecisionLookup = {
  record: DecisionRow | null;
  state: "loading" | "ready" | "missing";
};

/** Read one decision by id from the shared store — a source chat showing a decision that may
 *  already be answered, expired or withdrawn (#4725, Gap 2). Loads it once on mount; a cached
 *  record (or a prior 404) resolves with no request. */
export function useDecision(id: string): DecisionLookup {
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  useEffect(() => {
    void store.loadDecision(id);
  }, [id]);
  const record = snapshot.records[id] ?? null;
  const state: DecisionLookup["state"] = record ? "ready" : snapshot.missing[id] ? "missing" : "loading";
  return { record, state };
}

/** Test seam: exercise the shared store + its bus subscriptions without React. */
export const decisionsStoreForTest = store;
