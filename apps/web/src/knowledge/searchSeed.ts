// A one-shot handoff of a SEARCH TERM into the Knowledge surface, for palette rows that
// have to land the operator somewhere useful.
//
// The Knowledge store surface has no per-chunk anchor — no route, no selected-id state, no
// scroll target. Its list is whatever `/api/knowledge/search` returns for the term in its
// own `useState` search box. So "open this chunk" is not expressible; the closest honest
// thing is "open the surface already showing the search this row came from", which puts the
// picked chunk in the list the operator lands on. That is what this carries.
//
// A module-level box rather than uiStore state, because a search term is a one-shot HANDOFF
// and not console layout: uiStore is persisted, and a seeded query written there would
// survive a reload and silently re-narrow the surface on the operator's next visit. It is
// CONSUMED on read (`takeKnowledgeSearchSeed`) for the same reason — replaying it on every
// remount would fight whatever the operator typed since.
//
// A tiny external store, the shape `chat/publishDialogStore.ts` already uses for the same
// job (a trigger with no reference into the component it has to reach). The subscription is
// what covers BOTH arrival orders: the seed is usually set before the surface mounts (the
// palette routes to it), but it can also land while the surface is already open. The
// `useSyncExternalStore` wiring stays in here — the surface takes a hook, not three exports
// it has to re-assemble at the call site.

import { useSyncExternalStore } from "react";

let _seed: string | null = null;
let _version = 0;
const _listeners = new Set<() => void>();

/** Monotonic counter, bumped on every seed. Plain (non-hook) read, for tests and any
 *  non-React caller; `useKnowledgeSearchSeedVersion` is the subscribed one. */
export function knowledgeSearchSeedVersion(): number {
  return _version;
}

function subscribe(fn: () => void): () => void {
  _listeners.add(fn);
  return () => {
    _listeners.delete(fn);
  };
}

/** The seed version, subscribed — key an effect on it and `takeKnowledgeSearchSeed()` inside.
 *  The VERSION, not the value: re-seeding the same term has to re-apply (the operator picked
 *  that ⌘K row again after typing something else), and a value-keyed effect would not fire. */
export function useKnowledgeSearchSeedVersion(): number {
  return useSyncExternalStore(subscribe, knowledgeSearchSeedVersion, knowledgeSearchSeedVersion);
}

/** Queue a search term for the Knowledge surface's next render. */
export function seedKnowledgeSearch(query: string): void {
  _seed = query;
  _version += 1;
  // Snapshot: a listener may (un)subscribe while being notified.
  for (const l of [..._listeners]) l();
}

/** Read AND clear the pending term (null when there is none). */
export function takeKnowledgeSearchSeed(): string | null {
  const seed = _seed;
  _seed = null;
  return seed;
}
