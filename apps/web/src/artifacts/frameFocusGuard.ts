// Focus-theft guard for transcript-hosted frames (ADR 0118, #4076).
//
// An inline artifact (or a frame-rendered plugin component) is model-/plugin-written code. One
// that calls `window.focus()` or focuses an element on load pulls the console's keyboard focus
// into its iframe: the page's `activeElement` becomes the frame, so the next thing the operator
// types into the composer lands in the artifact instead. It can't press the send chip (that is
// console DOM), but it would receive the keystrokes.
//
// The guard: focus may enter a hosted frame only when the OPERATOR moved it there —
//   • a pointer press into the frame: the pointer is over the frame element (pointerenter/leave
//     on the iframe in THIS document — presses inside it are the frame's own events, but hover
//     is ours) AND the page has transient user activation, which a press inside a child frame
//     propagates up to its ancestors; or a pointerdown on the frame element itself;
//   • or a keyboard Tab / Shift+Tab that just moved focus along the page.
// Focus arriving any other way — the frame focusing itself — is handed straight back to the
// element that held it before (typically the composer), or, if that is gone, dropped off the
// frame.

import { useEffect, type RefObject } from "react";

/** How long after a Tab keydown / pointerdown a focus move into the frame still counts as the
 *  operator's own. */
const INTENT_WINDOW_MS = 1000;

let installed = false;
let lastTabAt = -Infinity;
let lastOutsideFocus: HTMLElement | null = null;

/** Page-wide trackers, installed once: the last Tab keydown, and the last element OUTSIDE any
 *  iframe that held focus — the one focus is returned to after a theft. */
function installTrackers(): void {
  if (installed || typeof document === "undefined") return;
  installed = true;
  document.addEventListener(
    "keydown",
    (e) => {
      if (e.key === "Tab") lastTabAt = Date.now();
    },
    true,
  );
  document.addEventListener(
    "focusin",
    (e) => {
      const t = e.target;
      if (t instanceof HTMLElement && t.tagName !== "IFRAME") lastOutsideFocus = t;
    },
    true,
  );
}

/** Whether the page has transient user activation right now. Where the API is missing, a hover
 *  alone has to stand in (older engines). */
function hasTransientActivation(): boolean {
  const ua = (navigator as Navigator & { userActivation?: { isActive: boolean } }).userActivation;
  return ua ? ua.isActive : true;
}

/** Hand focus back from `frame` after a theft: to the element that held it before, else off the
 *  frame entirely. */
function returnFocus(frame: HTMLIFrameElement): void {
  const prev = lastOutsideFocus;
  if (prev && prev.isConnected && prev !== frame) {
    try {
      prev.focus({ preventScroll: true });
    } catch {
      /* best effort */
    }
  }
  if (document.activeElement === frame) {
    try {
      frame.blur();
    } catch {
      /* best effort */
    }
  }
}

/** Keep the hosted frame in `frameRef` from taking keyboard focus on its own (see the header).
 *  `active` = the frame is mounted live. */
export function useFrameFocusGuard(frameRef: RefObject<HTMLIFrameElement | null>, active: boolean): void {
  useEffect(() => {
    if (!active) return;
    const frame = frameRef.current;
    if (!frame) return;
    installTrackers();
    let hovering = false;
    let pressedAt = -Infinity;
    const onEnter = () => {
      hovering = true;
    };
    const onLeave = () => {
      hovering = false;
    };
    const onPress = () => {
      pressedAt = Date.now();
    };
    // Focus moving INTO a child frame blurs this window; by the next task `activeElement` names
    // the frame that took it.
    let timer: ReturnType<typeof setTimeout> | null = null;
    const check = () => {
      timer = null;
      if (document.activeElement !== frame) return;
      const now = Date.now();
      const operatorMoved =
        now - lastTabAt < INTENT_WINDOW_MS ||
        now - pressedAt < INTENT_WINDOW_MS ||
        (hovering && hasTransientActivation());
      if (!operatorMoved) returnFocus(frame);
    };
    const onBlur = () => {
      if (timer === null) timer = setTimeout(check, 0);
    };
    frame.addEventListener("pointerenter", onEnter);
    frame.addEventListener("pointerleave", onLeave);
    frame.addEventListener("pointerdown", onPress);
    frame.addEventListener("focus", onBlur);
    window.addEventListener("blur", onBlur);
    return () => {
      if (timer !== null) clearTimeout(timer);
      frame.removeEventListener("pointerenter", onEnter);
      frame.removeEventListener("pointerleave", onLeave);
      frame.removeEventListener("pointerdown", onPress);
      frame.removeEventListener("focus", onBlur);
      window.removeEventListener("blur", onBlur);
    };
  }, [frameRef, active]);
}

/** Test seam: reset the page-wide trackers. */
export function resetFrameFocusGuardForTest(): void {
  lastTabAt = -Infinity;
  lastOutsideFocus = null;
}
