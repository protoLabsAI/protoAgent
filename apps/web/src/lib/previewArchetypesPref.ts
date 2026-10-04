// Settings ▸ New agent ▸ Advanced ▸ "Show preview archetypes" — whether the picker also lists
// the catalog's HELD archetypes (still being tested), badged "Preview". Off unless the operator
// turns it on, and never shown by default.
//
// Per-CONSOLE, like the editor pref (lib/editorPref): one un-suffixed localStorage key, every
// access wrapped — storage can throw (private mode, blocked site data) and the picker must
// render anyway, falling back to OFF. An in-memory copy keeps the choice for this page's
// lifetime when storage is unavailable.

import { useSyncExternalStore } from "react";

export const PREVIEW_ARCHETYPES_KEY = "protoagent.newAgent.showPreviewArchetypes";

const listeners = new Set<() => void>();
let memory: boolean | null = null;

export function getShowPreviewArchetypes(): boolean {
  try {
    const raw = globalThis.localStorage?.getItem(PREVIEW_ARCHETYPES_KEY);
    if (raw === "1") return true;
    if (raw === "0") return false;
  } catch {
    /* storage unavailable — fall through */
  }
  return memory ?? false;
}

export function setShowPreviewArchetypes(on: boolean): void {
  memory = on;
  try {
    globalThis.localStorage?.setItem(PREVIEW_ARCHETYPES_KEY, on ? "1" : "0");
  } catch {
    /* storage unavailable — the in-memory value still applies this session */
  }
  listeners.forEach((l) => l());
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  // Another window flipping it (the desktop app can hold several).
  const onStorage = (e: StorageEvent) => {
    if (e.key === PREVIEW_ARCHETYPES_KEY) cb();
  };
  try {
    globalThis.addEventListener?.("storage", onStorage);
  } catch {
    /* no window (SSR/test) */
  }
  return () => {
    listeners.delete(cb);
    try {
      globalThis.removeEventListener?.("storage", onStorage);
    } catch {
      /* ignore */
    }
  };
}

export function useShowPreviewArchetypes(): boolean {
  return useSyncExternalStore(subscribe, getShowPreviewArchetypes, getShowPreviewArchetypes);
}
