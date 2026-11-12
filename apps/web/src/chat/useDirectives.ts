import { useMemo, useSyncExternalStore } from "react";

import { chatStore } from "./chat-store";
import { api, ApiError } from "../lib/api";
import type { DirectiveList, DirectiveRow } from "../lib/api/chat";
import { onConnectionChange, onTopic } from "../lib/events";

export type DirectivesSnapshot = {
  state: "loading" | "ready" | "failed" | "stale";
  active: DirectiveRow[];
  history: DirectiveRow[];
  /** Epoch milliseconds of the last successful list load. */
  loadedAt: number | null;
};

export type RevokeResult =
  | { ok: true }
  // Null if the refresh failed or the closed row has since been forgotten.
  | { conflict: DirectiveRow | null }
  | { gone: true }
  | { error: unknown };

function createStore(sessionId: string) {
  let snapshot: DirectivesSnapshot = { state: "loading", active: [], history: [], loadedAt: null };
  const listeners = new Set<() => void>();
  const observers = new Set<() => void>();
  let stop: (() => void) | null = null;
  let pending: Promise<DirectiveList | null> | null = null;
  let dirty = false;

  // Coalesce invalidations during a request, but fetch again afterwards: the response
  // already in flight may predate the event (or the DELETE that just completed).
  function refresh(): Promise<DirectiveList | null> {
    dirty = true;
    if (pending) return pending;
    pending = (async () => {
      let result: DirectiveList | null = null;
      do {
        dirty = false;
        try {
          result = await api.listDirectives(sessionId);
          snapshot = {
            state: "ready",
            active: result.directives.filter((d) => d.status === "in_force"),
            history: result.directives
              .filter((d) => d.status !== "in_force")
              .sort((a, b) => (b.closed_at ?? "").localeCompare(a.closed_at ?? "")),
            loadedAt: Date.now(),
          };
        } catch {
          result = null;
          snapshot = { ...snapshot, state: snapshot.loadedAt === null ? "failed" : "stale" };
        }
        for (const listener of listeners) listener();
        for (const observer of observers) observer();
      } while (dirty);
      pending = null;
      return result;
    })();
    return pending;
  }

  async function revoke(id: string, reason?: string): Promise<RevokeResult> {
    try {
      const { directive } = await api.revokeDirective(sessionId, id, reason);
      // The server records the operator event in its checkpoint. Mirror the successful
      // response locally because an idle checkpoint append has no streamed chat turn.
      const session = chatStore.getSnapshot().sessions.find((s) => s.id === sessionId);
      const noteId = `directive-withdrawn-${id}`;
      if (session && !session.messages.some((m) => m.id === noteId)) {
        // Match graph.directives.withdrawal_note's bounded, first-line copy.
        const clip = (text: string) => text.length > 2000 ? text.slice(0, 2000).trimEnd() + "…" : text;
        const firstLine = clip(directive.quote.trim().split(/\r?\n/)[0]);
        const note = clip(reason?.trim() ?? "");
        chatStore.updateMessages(sessionId, [...session.messages, {
          id: noteId, role: "user", status: "done", createdAt: Date.now(),
          content: `You withdrew: "${firstLine}"${note ? ` — ${note}` : ""}`,
          protoagent_directive_withdrawn: { id },
        }]);
      }
    } catch (error) {
      if (error instanceof ApiError && (error.status === 409 || error.status === 404)) {
        const fresh = await refresh();
        if (error.status === 404) return { gone: true };
        return { conflict: fresh?.directives.find((d) => d.id === id && d.status !== "in_force") ?? null };
      }
      return { error };
    }
    await refresh();
    return { ok: true };
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
      const offTopic = onTopic("directive.changed", (data) => {
        if (data.session_id === sessionId) void refresh();
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

  // Inventory consumers observe cache changes without starting unbounded per-chat fetches.
  function observe(listener: () => void) {
    observers.add(listener);
    return () => { observers.delete(listener); };
  }
  return { subscribe, observe, getSnapshot: () => snapshot, refresh, revoke };
}

// Shared across consumers; only sessions with subscribers have live bus subscriptions.
// Like backgroundJobStore, snapshots survive a consumer unmount/remount.
const stores = new Map<string, ReturnType<typeof createStore>>();
export function directiveStore(sessionId: string) {
  let store = stores.get(sessionId);
  if (!store) {
    store = createStore(sessionId);
    stores.set(sessionId, store);
  }
  return store;
}

export function useDirectives(sessionId: string) {
  const store = useMemo(() => directiveStore(sessionId), [sessionId]);
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  return { ...snapshot, refresh: store.refresh, revoke: store.revoke };
}

/** Test seam: exercise the same shared subscriptions and actions without React. */
export const directivesStoreForTest = directiveStore;
