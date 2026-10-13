// Focus guard for transcript-hosted frames (ADR 0118, #4076): click-to-activate + quarantine.
//
// An inline artifact (or a frame-rendered plugin component) is model-/plugin-written code with
// scripts on. One that calls `focus()` on its own input pulls the console's keyboard focus into
// its iframe, so what the operator types into the composer lands in the artifact instead. It
// can't press the send chip (console DOM) and its CSP has `connect-src 'none'`, so this is a
// UX/integrity problem, not exfiltration — but a frame re-focusing itself in a 1 ms loop must not
// be able to keep the keyboard, and keyboard Tab into and through frames must keep working.
//
// What doesn't work, measured (#4154): `inert` (Chromium lets an inert frame's own script focus
// its own input; WebKit holds only until the frame was activated once), the
// `focus-without-user-activation` permission policy (ignored), and a reactive "hand focus back"
// guard (a 1 ms re-focus loop still captures keys between hand-backs).
//
// The model:
//   • Every guarded frame runs LIVE (charts and animations keep going) but starts INACTIVE. An
//     inactive frame sits under a transparent click-catcher (FrameActivationShield), so a press
//     on it lands in THIS document — a press inside a cross-origin frame never would.
//   • A frame becomes ACTIVE only by an operator gesture aimed at it: a pointerdown on its
//     shield, or a keyboard Tab / Shift+Tab whose traversal lands in it (a host Tab keydown sets
//     a short-lived intent that the next focus move into a frame consumes; headed Chrome runs
//     sandboxed frames out of process, so that handoff is a few ms late — hence a window, not
//     "the next task"). An active frame owns focus normally; it goes inactive again as soon as
//     focus returns to the page (a focusin on a page element, or this window regaining focus).
//   • Focus moving from the page into an INACTIVE frame with no such gesture is a theft. In the
//     same task the host blanks the frame (src → about:blank, which stops its scripts, so a 1 ms
//     loop dies on its first steal) and QUARANTINES it: the host renders "This artifact tried to
//     take keyboard focus." with a "Run it" button in its place, and hands focus back to the
//     element that held it (usually the composer) at once and again a task later — see
//     returnFocus for why both. The quarantine is kept in memory per frame key, so a re-render of the same
//     message keeps it; "Run it" remounts the frame already active (that click is the gesture).
//
// Limits: Escape can't deactivate (a key pressed inside a cross-origin frame never reaches this
// document; clicking or tabbing anywhere on the page does). And a move from one frame straight
// into another is invisible here (see onFrameFocused) — it never involves the composer.

import { useCallback, useEffect, useState, useSyncExternalStore, type RefObject } from "react";

/** How long after a host Tab keydown a focus move into a frame still counts as that Tab's
 *  traversal. Out-of-process frames (headed Chrome) hand focus over a few ms late; 300 ms is
 *  generous for that and far shorter than a human's next keystroke. */
export const TAB_INTENT_MS = 300;

type Entry = {
  key: string;
  active: boolean;
  quarantined: boolean;
  onActiveChange: (active: boolean) => void;
};

const frames = new Map<HTMLIFrameElement, Entry>();
let installed = false;
let tabIntent: { event: KeyboardEvent; at: number } | null = null;
/** The last element OUTSIDE any iframe that held focus — where focus goes back after a theft. */
let lastOutsideFocus: HTMLElement | null = null;

const isFrame = (el: Element | null): el is HTMLIFrameElement => !!el && el.tagName === "IFRAME";

// ── quarantine store (per frame key, in memory for the session) ─────────────────────────

const quarantinedKeys = new Set<string>();
/** Keys whose next mount starts ACTIVE — set by "Run it", consumed on register. */
const runRequested = new Set<string>();
const listeners = new Set<() => void>();
const emit = () => {
  for (const fn of [...listeners]) fn();
};
function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

// ── page-wide trackers ───────────────────────────────────────────────────────────────

function setActive(entry: Entry, active: boolean): void {
  if (entry.active === active) return;
  entry.active = active;
  entry.onActiveChange(active);
}

function deactivateAll(): void {
  for (const entry of frames.values()) setActive(entry, false);
}

/** Hand focus back from `frame` after a theft, to the element that held it before (usually the
 *  composer). Twice:
 *   1. NOW, inside the window `blur` dispatch that reported the theft. In headed Chrome (frames
 *      out of process) this is what closes the gap: waiting a task first left the keyboard with
 *      the frame for ~10 ms after every steal — every key typed in that window was lost (#4076
 *      F4, measured: a 1 ms self-focus loop after "Run it" + a composer click lost 0–24 keys
 *      per run at full synthetic speed; with this, 0).
 *   2. Again one task later. Some engines (WebKit, Chromium in-process) override a focus() made
 *      during that dispatch with the in-flight move into the frame, so keys keep going to the
 *      frame while `activeElement` already claims the composer — and in headed Chrome a
 *      microtask is still too early. A task later the move has settled; if the page still
 *      doesn't have focus, blur + focus forces a real move (focus() on the element that is
 *      already `activeElement` is a no-op). */
function returnFocus(frame: HTMLIFrameElement): void {
  const prev = lastOutsideFocus;
  const restore = () => {
    if (!prev || !prev.isConnected) return;
    try {
      prev.focus({ preventScroll: true });
    } catch {
      /* best effort */
    }
  };
  restore();
  setTimeout(() => {
    const ae = document.activeElement;
    // Focus already moved on to something real (the operator clicked elsewhere): leave it.
    if (ae && ae !== frame && ae !== document.body && ae !== prev) return;
    if (ae === prev) {
      if (document.hasFocus()) return; // the immediate hand-back stuck
      try {
        prev?.blur();
      } catch {
        /* best effort */
      }
    }
    restore();
  }, 0);
}

/** A guarded inactive frame took focus with no gesture: stop it, quarantine it, hand focus back. */
function quarantine(frame: HTMLIFrameElement, entry: Entry): void {
  returnFocus(frame);
  if (entry.quarantined) return; // a second steal before the blank navigation commits
  entry.quarantined = true;
  try {
    // Stop the frame's scripts now, in this task, before React gets to unmount it (a 1 ms loop
    // would otherwise keep stealing until the re-render).
    frame.src = "about:blank";
  } catch {
    /* best effort */
  }
  quarantinedKeys.add(entry.key);
  emit();
}

/** Whether the pending Tab licenses a focus move into a frame now — consumed if so. A Tab the
 *  page consumed (`preventDefault`, e.g. slash autocomplete in the composer) never counts. */
function consumeTabIntent(): boolean {
  const tab = tabIntent;
  tabIntent = null;
  return !!tab && !tab.event.defaultPrevented && Date.now() - tab.at < TAB_INTENT_MS;
}

/** Focus just moved from this document into `frame` (seen on the window blur).
 *
 *  Only a move OUT OF THIS DOCUMENT is visible here. Focus moving from one child frame straight
 *  into another — a Tab out of an active frame into the next, or one frame's script focus() while
 *  the operator types in another — fires nothing on this page, and Chromium doesn't even update
 *  `activeElement` for the script case (measured). So a frame can't be judged for that move; the
 *  composer, which is what this guard protects, is never involved in it. */
function onFrameFocused(frame: HTMLIFrameElement): void {
  // Another active frame loses its claim the moment focus leaves it.
  for (const [f, e] of frames) if (f !== frame) setActive(e, false);
  const entry = frames.get(frame);
  if (!entry) return; // not a guarded frame (e.g. a side-panel plugin view)
  if (entry.quarantined) {
    returnFocus(frame);
    return;
  }
  if (entry.active) return;
  if (consumeTabIntent()) {
    setActive(entry, true);
    return;
  }
  quarantine(frame, entry);
}

function installTrackers(): void {
  if (installed || typeof document === "undefined") return;
  installed = true;
  const current = document.activeElement;
  if (current instanceof HTMLElement && !isFrame(current) && current !== document.body) lastOutsideFocus = current;
  document.addEventListener(
    "keydown",
    (e) => {
      if (e.key === "Tab" && !e.altKey && !e.ctrlKey && !e.metaKey) tabIntent = { event: e, at: Date.now() };
    },
    true,
  );
  document.addEventListener(
    "focusin",
    (e) => {
      const t = e.target;
      if (t instanceof HTMLElement && !isFrame(t)) {
        lastOutsideFocus = t;
        tabIntent = null; // the Tab's traversal landed on a page element
        deactivateAll();
      }
    },
    true,
  );
  window.addEventListener("focus", () => {
    if (!isFrame(document.activeElement)) deactivateAll();
  });
  // Focus moving INTO a child frame blurs this window; at that point `activeElement` already
  // names the frame (Chromium in- and out-of-process, WebKit — measured), so the verdict and
  // the hand-back happen in this same task.
  window.addEventListener("blur", () => {
    const el = document.activeElement;
    if (isFrame(el)) onFrameFocused(el);
  });
}

function register(frame: HTMLIFrameElement, entry: Entry): () => void {
  installTrackers();
  frames.set(frame, entry);
  return () => {
    if (frames.get(frame) === entry) frames.delete(frame);
  };
}

// Track from the start, not from the first frame's mount: the composer is usually focused long
// before an artifact appears, and it is where a theft hands focus back to.
installTrackers();

// ── the hook ─────────────────────────────────────────────────────────────────────────

export type FrameGuard = {
  /** The frame may hold keyboard focus (the operator clicked or tabbed into it). */
  active: boolean;
  /** The frame took focus on its own: render the quarantine placeholder, not the frame. */
  quarantined: boolean;
  /** Activate on the operator's press (the shield's pointerdown) and hand the frame focus. */
  activate: () => void;
  /** "Run it": lift the quarantine and remount the frame already active. */
  run: () => void;
};

/** Guard the hosted frame in `frameRef` (see the header). `key` identifies the frame across
 *  re-renders (its quarantine survives one); `live` = the frame is mounted. */
export function useFrameFocusGuard(
  frameRef: RefObject<HTMLIFrameElement | null>,
  key: string,
  live: boolean,
): FrameGuard {
  const quarantined = useSyncExternalStore(
    subscribe,
    () => quarantinedKeys.has(key),
    () => false,
  );
  const [active, setActiveState] = useState(false);
  const mountedLive = live && !quarantined;

  useEffect(() => {
    if (!mountedLive) return;
    const frame = frameRef.current;
    if (!frame) return;
    const startActive = runRequested.delete(key);
    setActiveState(startActive);
    return register(frame, {
      key,
      active: startActive,
      quarantined: false,
      onActiveChange: setActiveState,
    });
  }, [frameRef, key, mountedLive]);

  const activate = useCallback(() => {
    const frame = frameRef.current;
    if (!frame) return;
    const entry = frames.get(frame);
    if (!entry || entry.quarantined) return;
    setActive(entry, true);
    try {
      frame.focus({ preventScroll: true });
    } catch {
      /* best effort */
    }
  }, [frameRef]);

  const run = useCallback(() => {
    runRequested.add(key);
    if (quarantinedKeys.delete(key)) emit();
  }, [key]);

  return { active: mountedLive && active, quarantined, activate, run };
}

/** Test seam: reset the page-wide trackers and the quarantine store. */
export function resetFrameFocusGuardForTest(): void {
  tabIntent = null;
  lastOutsideFocus = null;
  frames.clear();
  quarantinedKeys.clear();
  runRequested.clear();
  emit();
}
