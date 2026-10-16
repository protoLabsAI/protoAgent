// Settings ▸ Chat ▸ "Save summaries when closing tabs in bulk" — whether the tab menu's bulk
// closers (Close others / left / right) harvest a summary of each chat before purging it. Off
// unless the operator turns it on: bulk close is a cleanup gesture, so by default it saves
// nothing (like Shift+click quick-delete, #4273). Incognito chats are never harvested either
// way (defaultMemoryChoice enforces that).
//
// Per-CONSOLE, like the preview-archetypes pref (lib/previewArchetypesPref): one un-suffixed
// localStorage key, every access wrapped — storage can throw (private mode, blocked site data)
// and the tab menu must keep working, falling back to OFF. An in-memory copy keeps the choice
// for this page's lifetime when storage is unavailable.

import { useSyncExternalStore } from "react";
import { readKey, writeKey } from "./storage";

export const BULK_CLOSE_HARVEST_KEY = "protoagent.chat.bulkCloseHarvest";

const listeners = new Set<() => void>();
let memory: boolean | null = null;

export function getBulkCloseHarvest(): boolean {
  try {
    const raw = readKey("local", BULK_CLOSE_HARVEST_KEY);
    if (raw === "1") return true;
    if (raw === "0") return false;
  } catch {
    /* storage unavailable — fall through */
  }
  return memory ?? false;
}

export function setBulkCloseHarvest(on: boolean): void {
  memory = on;
  try {
    writeKey("local", BULK_CLOSE_HARVEST_KEY, on ? "1" : "0");
  } catch {
    /* storage unavailable — the in-memory value still applies this session */
  }
  listeners.forEach((l) => l());
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  // Another window flipping it (the desktop app can hold several).
  const onStorage = (e: StorageEvent) => {
    if (e.key === BULK_CLOSE_HARVEST_KEY) cb();
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

export function useBulkCloseHarvest(): boolean {
  return useSyncExternalStore(subscribe, getBulkCloseHarvest, getBulkCloseHarvest);
}
