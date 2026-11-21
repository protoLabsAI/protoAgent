/**
 * The instance-wide standing orders (ADR 0135 D13; work-view-additions §3) — ONE shared store
 * for the whole console, the instance-ledger sibling of the per-chat `directiveStore`.
 *
 * It holds EVERY row `GET /api/directives` returns, closed ones too: the Work group filters to
 * in-force on the client, and a row that closed under an open detail (a 409) must stay listed
 * so that detail stays mounted, read-only, with its "already closed" notice. `useWorkItems`
 * merges these rows into the Your instructions source and drives the reads; the directives
 * section withdraws through {@link instanceDirectiveStore.withdraw}.
 */
import { api, ApiError } from "../lib/api";
import type { DirectiveList, DirectiveRow } from "../lib/api/chat";
import type { RevokeResult } from "../chat/useDirectives";

export type InstanceDirectivesSnapshot = {
  state: "loading" | "ready" | "failed" | "stale";
  /** Every instance-wide row, oldest first, whatever its status. */
  rows: DirectiveRow[];
  /** Epoch milliseconds of the last successful list load. */
  loadedAt: number | null;
};

const INITIAL: InstanceDirectivesSnapshot = { state: "loading", rows: [], loadedAt: null };
let snapshot: InstanceDirectivesSnapshot = INITIAL;
const listeners = new Set<() => void>();
let pending: Promise<DirectiveList | null> | null = null;
let dirty = false;

function emit() {
  for (const listener of listeners) listener();
}

// Coalesce invalidations during a request, but fetch again afterwards: the response already
// in flight may predate the event (or the withdraw that just completed).
function refresh(): Promise<DirectiveList | null> {
  dirty = true;
  if (pending) return pending;
  pending = (async () => {
    let result: DirectiveList | null = null;
    do {
      dirty = false;
      try {
        result = await api.listInstanceDirectives();
        snapshot = { state: "ready", rows: result.directives, loadedAt: Date.now() };
      } catch {
        result = null;
        snapshot = { ...snapshot, state: snapshot.loadedAt === null ? "failed" : "stale" };
      }
      emit();
    } while (dirty);
    pending = null;
    return result;
  })();
  return pending;
}

/** Withdraw one order everywhere. On success the returned (now closed) row replaces the
 *  listed one and `onWithdrawn` runs in the SAME synchronous step, so the caller's own state
 *  change (closing the detail) lands in the render that drops the row and the count. A 409 or
 *  404 re-reads the list first, exactly like the chat ledger's revoke. */
async function withdraw(id: string, reason?: string, onWithdrawn?: () => void): Promise<RevokeResult> {
  let directive: DirectiveRow;
  try {
    ({ directive } = await api.withdrawInstanceDirective(id, reason));
  } catch (error) {
    if (error instanceof ApiError && (error.status === 409 || error.status === 404)) {
      const fresh = await refresh();
      if (error.status === 404) return { gone: true };
      return { conflict: fresh?.directives.find((d) => d.id === id && d.status !== "in_force") ?? null };
    }
    return { error };
  }
  snapshot = { ...snapshot, rows: snapshot.rows.map((row) => (row.id === id ? directive : row)) };
  emit();
  onWithdrawn?.();
  void refresh();
  return { ok: true };
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export const instanceDirectiveStore = {
  subscribe,
  getSnapshot: () => snapshot,
  refresh,
  withdraw,
  /** Test seam: forget the rows and load state between cases. */
  resetForTest() {
    snapshot = INITIAL;
    pending = null;
    dirty = false;
  },
};
