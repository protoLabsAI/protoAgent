// Settings ▸ Chat "Open files in" — which editor a file path in tool output opens in.
//
// Per-VIEWER, deliberately outside the persisted UI store: that store is namespaced per
// fleet agent (uiStore `_layoutStorage`), and the editor installed on this machine doesn't
// change when you switch agents. One un-suffixed localStorage key, every access wrapped —
// storage can throw (private mode, blocked site data) and the console must render anyway,
// falling back to the default.

import { useSyncExternalStore } from "react";

import { DEFAULT_EDITOR, isEditorId, type EditorId } from "./editorLinks";

export const EDITOR_PREF_KEY = "protoagent.editor";

const listeners = new Set<() => void>();
// In-memory fallback so the choice still sticks for this page's lifetime when storage throws.
let memory: EditorId | null = null;

export function getEditorPref(): EditorId {
  try {
    const raw = globalThis.localStorage?.getItem(EDITOR_PREF_KEY);
    if (isEditorId(raw)) return raw;
  } catch {
    /* storage unavailable — fall through */
  }
  return memory ?? DEFAULT_EDITOR;
}

export function setEditorPref(editor: EditorId): void {
  memory = editor;
  try {
    globalThis.localStorage?.setItem(EDITOR_PREF_KEY, editor);
  } catch {
    /* storage unavailable — the in-memory value still applies this session */
  }
  listeners.forEach((l) => l());
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  // Another tab/window changing the pref (the desktop app can hold several windows).
  const onStorage = (e: StorageEvent) => {
    if (e.key === EDITOR_PREF_KEY) cb();
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

export function useEditorPref(): EditorId {
  return useSyncExternalStore(subscribe, getEditorPref, getEditorPref);
}
