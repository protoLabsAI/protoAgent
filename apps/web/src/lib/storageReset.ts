// The "storage was just reset" signal (ADR 0114 D6), shared by every transcript writer (the
// chat store, palette/DM threads) so none of them can write cleared data straight back.
//
// Two ways a page learns it:
// - It IS the crash page that ran "Free up space & reload": AppCrash sets the in-realm flag
//   `globalThis.__protoagentNoFlush` before clearing (it may not import the stores), so this
//   page's pending timers and pagehide flushes write nothing.
// - ANOTHER tab broadcast `{type: "storage-reset"}` on BroadcastChannel("protoagent.storage"):
//   this tab's in-memory transcripts are now stale. It stops persisting and reloads, dropping
//   the dirty state (the server rebuilds the last 24 h; ADR 0104). The flag is module state, so
//   it can't outlive that reload.
//
// Import-free: AppCrash uses `broadcastStorageReset`.

export const STORAGE_RESET_CHANNEL = "protoagent.storage";

type ResetGlobals = { __protoagentNoFlush?: boolean };

let stopped = false;
let reloadPage: () => void = () => {
  try {
    window.location.reload();
  } catch {
    /* non-browser */
  }
};

/** True once this page must not persist transcripts any more. */
export function persistBlocked(): boolean {
  return stopped || (globalThis as ResetGlobals).__protoagentNoFlush === true;
}

/** The crash page's half: block this page's own writes (set BEFORE clearing). */
export function blockPersistInThisPage(): void {
  (globalThis as ResetGlobals).__protoagentNoFlush = true;
}

/** Tell every other tab its in-memory transcripts are stale. Feature-detected. */
export function broadcastStorageReset(): void {
  try {
    if (typeof BroadcastChannel === "undefined") return;
    const ch = new BroadcastChannel(STORAGE_RESET_CHANNEL);
    ch.postMessage({ type: "storage-reset" });
    ch.close();
  } catch {
    /* best-effort */
  }
}

/** Receiver: stop persisting, then reload so the stale state is dropped. The sender page
 *  (already flagged) ignores its own broadcast — it may be showing the largest-keys list. */
export function handleStorageReset(data: unknown): void {
  if ((data as { type?: string } | null)?.type !== "storage-reset") return;
  if ((globalThis as ResetGlobals).__protoagentNoFlush === true) return;
  stopped = true;
  reloadPage();
}

try {
  if (typeof BroadcastChannel !== "undefined") {
    const ch = new BroadcastChannel(STORAGE_RESET_CHANNEL);
    ch.onmessage = (e: MessageEvent) => handleStorageReset(e.data);
    (ch as { unref?: () => void }).unref?.(); // Node (tests): don't hold the loop open
  }
} catch {
  /* no BroadcastChannel — the in-realm flag still covers the crashed page itself */
}

/** Test-only: swap the reload and clear the stop. */
export function __setStorageResetReloadForTests(fn: (() => void) | null): void {
  stopped = false;
  if (fn) reloadPage = fn;
}
