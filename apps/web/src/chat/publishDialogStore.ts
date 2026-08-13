// Which session's publish dialog is open (#2179 P2, #2682) — a tiny external store
// (mirrors chat-store.ts's useSyncExternalStore shape) rather than ChatSurface-local
// state, so the trigger (a slash command or a tab context-menu item, neither of which
// hold a reference into ChatSurface's component instance) can open it directly. One
// dialog host is mounted once; this just says which session it's showing, if any.

import { useSyncExternalStore } from "react";

let openSessionId: string | null = null;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

export function openPublishDialog(sessionId: string): void {
  openSessionId = sessionId;
  emit();
}

export function closePublishDialog(): void {
  openSessionId = null;
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Plain (non-hook) read — for tests and any non-React caller. */
export function getPublishDialogSessionId(): string | null {
  return openSessionId;
}

export function usePublishDialogSessionId(): string | null {
  return useSyncExternalStore(subscribe, getPublishDialogSessionId, () => null);
}
