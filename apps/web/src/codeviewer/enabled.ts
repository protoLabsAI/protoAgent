import { useSyncExternalStore } from "react";

import type { RuntimeStatus } from "../lib/types";

// Is the code pane on for the agent this window is connected to? (ADR 0112 amendment.)
//
// The pane is an opt-in TOOLSET — `filesystem.code_pane`, default off, per agent — and the
// server reports it as `/api/runtime/status` `code_pane.enabled`. Off, the console has no Code
// surface, no "protoAgent" choice in Settings ▸ Chat ▸ Open files in, no follow mode, and file
// links go to the external editor; a `code-ref` chip from history renders inert.
//
// A module store (not only a query read) because the pane's non-React callers — the live
// stream hooks (live.ts), openCode, the file linker — must answer synchronously. App syncs it
// (`setCodePaneEnabled`) from the runtime status it already polls, so each fleet member's
// window reads its own agent's toggle, and a settings save that invalidates the status query
// flips it without a reload. Unknown (before the first status) reads as OFF: a link that
// briefly goes to the editor is harmless, a pane that opens against 404 routes is not.

let enabled = false;
const listeners = new Set<() => void>();

/** `code_pane.enabled` out of a runtime status; absent (an older server, or no status yet) = off. */
export function codePaneEnabledFrom(runtime: Pick<RuntimeStatus, "code_pane"> | null | undefined): boolean {
  return runtime?.code_pane?.enabled === true;
}

export function isCodePaneEnabled(): boolean {
  return enabled;
}

export function setCodePaneEnabled(next: boolean): void {
  if (next === enabled) return;
  enabled = next;
  listeners.forEach((l) => l());
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function useCodePaneEnabled(): boolean {
  return useSyncExternalStore(subscribe, isCodePaneEnabled, isCodePaneEnabled);
}
