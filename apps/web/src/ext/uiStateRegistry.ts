import { create, type StoreApi, type UseBoundStore } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

// Build-time fork seam for UI-STATE SLICES (ADR 0061, extends ADR 0038 D3). A fork calls
// `createUISlice(namespace, initial)` to own a namespaced, PERSISTED zustand store for its
// own UI state — WITHOUT editing core `uiStore.ts`, so `git pull upstream` stays conflict-
// free. This deliberately does NOT merge into the core `UIState` object (zustand has no
// runtime slice-merge, and a fork's state doesn't belong in core's closed shape); it gives
// the fork its OWN store, standardized: same per-agent persistence as core layout (ADR 0042),
// first-registration-wins (HMR-safe — re-calling with a namespace returns the SAME store).
//
// A fork uses it like any zustand hook:
//   const useMyState = createUISlice("myplugin", { panelOpen: false });
//   const open = useMyState((s) => s.panelOpen);            // in a component
//   useMyState.setState({ panelOpen: true });               // anywhere
// Core UI/layout state stays in `state/uiStore.ts` (it's core's, not a fork slice).

// Per-agent key (ADR 0042), mirroring uiStore: each fleet agent (URL slug) keeps its own
// slice state; host window = the bare key.
const _agent = (() => {
  try {
    const m = globalThis.location?.pathname?.match(/\/agent\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  } catch {
    return "";
  }
})();
const _storage = createJSONStorage(() => ({
  getItem: (name: string) => globalThis.localStorage.getItem(_agent ? `${name}:${_agent}` : name),
  setItem: (name: string, value: string) =>
    globalThis.localStorage.setItem(_agent ? `${name}:${_agent}` : name, value),
  removeItem: (name: string) => globalThis.localStorage.removeItem(_agent ? `${name}:${_agent}` : name),
}));

const _stores = new Map<string, unknown>();

/** Create (or, for an already-used namespace, return) a persisted, namespaced UI-state store.
 *  First-wins per namespace, so re-imports / HMR keep the same store instance + state. */
export function createUISlice<T extends object>(namespace: string, initial: T): UseBoundStore<StoreApi<T>> {
  const ns = (namespace || "").trim();
  if (!ns) throw new Error("createUISlice requires a non-empty namespace");
  const cached = _stores.get(ns) as UseBoundStore<StoreApi<T>> | undefined;
  if (cached) return cached; // first-wins / HMR-safe: same instance + state per namespace
  const store = create<T>()(persist(() => ({ ...initial }), { name: `proto:uislice:${ns}`, storage: _storage }));
  _stores.set(ns, store);
  return store;
}

/** The namespaces with a created slice (for inspection / devtools). */
export function registeredUISlices(): string[] {
  return [..._stores.keys()];
}
