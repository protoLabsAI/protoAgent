// Palette OUTCOME handoff — the launcher's other cross-window sink (ADR 0057).
//
// `nav.ts` forwards where the palette wants to GO. This forwards what a row that ran
// something came BACK with, and it exists for the same structural reason: the frameless
// desktop launcher is a separate window that HIDES itself the moment a row fires, so
// anything it renders after that lands in a webview nobody can see.
//
// Split out of the old monolithic `usePaletteRegistry.ts` alongside the rest of the adapter
// (#3289); the public entry points are re-exported verbatim from `../usePaletteRegistry`,
// so nothing that imports them had to change.
import { useEffect } from "react";

import { emit, hasDesktopShell, invoke, listen } from "../../lib/desktop";

/** A palette row's transient outcome, as it crosses the window boundary. Structurally the
 *  DS toast payload, so either side can hand it straight to `useToast()`. */
export type PaletteNotice = { tone: "success" | "error"; title?: string; message: string };

/** The event a launcher row's outcome rides to the console window. */
export const PALETTE_NOTICE_EVENT = "palette:notify";

/** The launcher's `notify` sink — the SAME handoff its navigation uses, for the same reason.
 *
 *  A `tool`/`emit` row closes the palette when it fires, and on the launcher closing the
 *  palette HIDES the window (`onOpenChange(false)` → `hide_launcher` → `window.hide()`). The
 *  webview stays alive, so the request completes and the toast renders — into a window
 *  nobody can see. That is the "reports its outcome instead of failing silently" claim
 *  failing silently. So the outcome goes where the operator's durable surface is: forwarded
 *  to the console window, which is raised the way a `navigate` row already raises it, so the
 *  message lands somewhere they are actually looking. Outside the desktop shell (a test, a
 *  fork surface) there is no other window — fall back to the local toast. */
export function forwardPaletteNotice(local: (n: PaletteNotice) => void): (n: PaletteNotice) => void {
  return (n) => {
    if (!hasDesktopShell()) {
      local(n);
      return;
    }
    void emit(PALETTE_NOTICE_EVENT, n);
    void invoke("focus_main");
  };
}

/** Read a forwarded notice off the wire, or `null`. Validated rather than cast: it arrives
 *  as an untyped Tauri event payload, and a toast built from `undefined` is a blank card the
 *  operator cannot act on. An unrecognized `tone` reads as an error — a row whose outcome
 *  we cannot classify is not a success. */
export function paletteNoticeFrom(raw: unknown): PaletteNotice | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const message = typeof r.message === "string" ? r.message.trim() : "";
  if (!message) return null;
  return {
    tone: r.tone === "success" ? "success" : "error",
    ...(typeof r.title === "string" && r.title.trim() ? { title: r.title.trim() } : {}),
    message,
  };
}

/** The console window's half: toast what a LAUNCHER row's `tool`/`emit` call came back
 *  with. The launcher has already raised this window (`focus_main`) by the time this fires,
 *  so the message lands where the operator is looking. A no-op outside the desktop shell. */
export function useForwardedPaletteNotices(notify: (n: PaletteNotice) => void): void {
  useEffect(() => {
    // `listen` resolves ASYNCHRONOUSLY, so cleanup can run before there is anything to clean
    // up. Assigning `off` in `.then` and calling it from the teardown loses that race: the
    // teardown no-ops, the listener registers a tick later, and nothing ever removes it.
    // Under StrictMode's mount/unmount/mount that leaves TWO listeners and every forwarded
    // notice toasts twice. The `cancelled` latch makes the unsubscribe idempotent in both
    // orders — unlisten now if we have it, or the moment it arrives.
    let cancelled = false;
    let off: (() => void) | null = null;
    void listen<unknown>(PALETTE_NOTICE_EVENT, (raw) => {
      const notice = paletteNoticeFrom(raw);
      if (notice) notify(notice);
    }).then((fn) => {
      if (cancelled) fn();
      else off = fn;
    });
    return () => {
      cancelled = true;
      off?.();
      off = null;
    };
  }, [notify]);
}
