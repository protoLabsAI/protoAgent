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

// ── "Open files in" (ADR 0112) ──────────────────────────────────────────────────────────
// A click on a file path opens either the in-app code pane ("protoagent", the default) or the
// external editor above ("editor"). Kept as its OWN key so the external editor stays
// remembered while the pane is the click target — it's what ⌘-click and the pane's ↗ use.

export type OpenFilesIn = "protoagent" | "editor";

export const OPEN_FILES_IN_KEY = "protoagent.openFilesIn";

let openInMemory: OpenFilesIn | null = null;

export function getOpenFilesIn(): OpenFilesIn {
  try {
    const raw = globalThis.localStorage?.getItem(OPEN_FILES_IN_KEY);
    if (raw === "protoagent" || raw === "editor") return raw;
    // An operator who explicitly turned file links OFF before the pane existed keeps them
    // off: their stored "off" meant "no links", and a new default must not overrule it.
    if (openInMemory) return openInMemory;
    if (globalThis.localStorage?.getItem(EDITOR_PREF_KEY) === "off") return "editor";
  } catch {
    /* storage unavailable — fall through */
  }
  return openInMemory ?? "protoagent";
}

export function setOpenFilesIn(v: OpenFilesIn): void {
  openInMemory = v;
  try {
    globalThis.localStorage?.setItem(OPEN_FILES_IN_KEY, v);
  } catch {
    /* storage unavailable — the in-memory value still applies this session */
  }
  listeners.forEach((l) => l());
}

export function useOpenFilesIn(): OpenFilesIn {
  return useSyncExternalStore(subscribeAny, getOpenFilesIn, getOpenFilesIn);
}

function subscribeAny(cb: () => void): () => void {
  const off = subscribe(cb);
  const onStorage = (e: StorageEvent) => {
    if (e.key === OPEN_FILES_IN_KEY) cb();
  };
  try {
    globalThis.addEventListener?.("storage", onStorage);
  } catch {
    /* no window */
  }
  return () => {
    off();
    try {
      globalThis.removeEventListener?.("storage", onStorage);
    } catch {
      /* ignore */
    }
  };
}

/** The single "Open files in" choice the Settings select shows: the pane, or an editor id
 *  (incl. "off"). */
export type OpenFilesChoice = "protoagent" | EditorId;

export function setOpenFilesChoice(choice: OpenFilesChoice): void {
  if (choice === "protoagent") {
    setOpenFilesIn("protoagent");
    return;
  }
  setEditorPref(choice);
  setOpenFilesIn("editor");
}
