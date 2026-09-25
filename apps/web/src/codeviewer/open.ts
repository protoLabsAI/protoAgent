import { flushSync } from "react-dom";

import { openView } from "../app/palette/nav";
import { useUI } from "../state/uiStore";
import { showCodeRef, useCodeViewer, type CodeRef } from "./store";

// Routing the code pane onto a dock (ADR 0112). The store (store.ts) says WHAT to show; this
// says WHERE — and is the one place the placement rule, the one-time widen and the mobile
// no-auto-open rule live, so every opener (tool-card link, code-ref chip, palette, follow)
// behaves the same.

export const CODE_SURFACE_ID = "code";

/** The width the right dock is widened to on the pane's first open — a code line needs more
 *  than the 360 default. Inside the DS AppShell's 280–720 bounds, set ONCE, never re-clamped
 *  (PROTO.md: the AppShell width is controlled; re-clamping breaks drag-to-collapse). */
export const CODE_PANE_WIDTH = 560;
const WIDENED_KEY = "protoagent.codePane.widened";

/** Minimum gap between two follow-mode jumps (ms). */
export const FOLLOW_THROTTLE_MS = 800;

const MOBILE_QUERY = "(max-width: 767px)";

function isMobileViewport(): boolean {
  try {
    return typeof window !== "undefined" && window.matchMedia(MOBILE_QUERY).matches;
  } catch {
    return false;
  }
}

type Dock = "left" | "right" | "bottom";

function dockOf(id: string): Dock | null {
  const ro = useUI.getState().railOrder;
  if (ro.left.includes(id)) return "left";
  if (ro.right.includes(id)) return "right";
  if (ro.bottom.includes(id)) return "bottom";
  return null;
}

/** Placement rule: the pane opens on a dock that does NOT hold chat — the point is reading
 *  the code NEXT TO the conversation, and on chat's own dock it would swap chat out. Moves
 *  the surface only when it sits on chat's dock (or nowhere); an operator who dragged it to
 *  another non-chat dock keeps their choice. Exported for tests. */
export function placeCodeSurface(): Dock {
  const chat = dockOf("chat") ?? "left";
  const code = dockOf(CODE_SURFACE_ID);
  if (code && code !== chat) return code;
  const target: Dock = chat === "right" ? "left" : "right";
  useUI.getState().moveSurface(CODE_SURFACE_ID, target);
  return target;
}

function widenedBefore(): boolean {
  try {
    return globalThis.localStorage?.getItem(WIDENED_KEY) === "1";
  } catch {
    return false;
  }
}

/** Widen the right dock ONCE (first open ever, on this device) — after that the operator's
 *  own drag wins, forever. Only ever grows it; never touches it per render. */
function widenOnce(dock: Dock): void {
  if (dock !== "right" || widenedBefore()) return;
  try {
    globalThis.localStorage?.setItem(WIDENED_KEY, "1");
  } catch {
    /* storage blocked — at worst we widen once per page load */
  }
  const ui = useUI.getState();
  if (ui.rightWidth < CODE_PANE_WIDTH) ui.setRightWidth(CODE_PANE_WIDTH);
}

export type OpenCodeOptions = {
  /** An open the operator did NOT ask for (the agent's show_code). Never takes over a phone
   *  screen (ADR 0086): on mobile it only seeds the pane; the chip pushes it on a tap. */
  auto?: boolean;
};

/** Show `ref` in the code pane and bring the pane on screen. */
export function openCode(ref: CodeRef, opts: OpenCodeOptions = {}): void {
  if (!showCodeRef(ref)) return;
  const mobile = isMobileViewport();
  if (opts.auto && mobile) return;
  if (!mobile) widenOnce(placeCodeSurface());
  // flushSync: a collapsed dock UNMOUNTS its column (DS AppShell), so the pane only exists
  // after this commit — committing now lets the pane's scroll-to-line run against a real DOM.
  try {
    flushSync(() => openView(CODE_SURFACE_ID));
  } catch {
    openView(CODE_SURFACE_ID);
  }
}

// ── Follow mode ─────────────────────────────────────────────────────────────────────────
let lastJump = 0;
let pendingTimer: ReturnType<typeof setTimeout> | null = null;
let pendingRef: CodeRef | null = null;

/** A live tool call touched `ref` — move the pane there if follow is on and not pinned.
 *  Throttled: at most one jump per FOLLOW_THROTTLE_MS, trailing, so a burst of reads lands
 *  on the LAST one instead of strobing through all of them. Desktop only, and it never
 *  re-routes docks: follow updates the pane the operator already opened. */
export function followCode(ref: Omit<CodeRef, "source">, now: number = Date.now()): void {
  const s = useCodeViewer.getState();
  if (!s.follow || s.pinned || isMobileViewport()) return;
  const next: CodeRef = { ...ref, source: "follow" };
  const wait = lastJump + FOLLOW_THROTTLE_MS - now;
  if (wait <= 0 && !pendingTimer) {
    lastJump = now;
    showCodeRef(next);
    return;
  }
  pendingRef = next;
  if (pendingTimer) return;
  pendingTimer = setTimeout(
    () => {
      pendingTimer = null;
      const r = pendingRef;
      pendingRef = null;
      const st = useCodeViewer.getState();
      if (!r || !st.follow || st.pinned) return;
      lastJump = Date.now();
      showCodeRef(r);
    },
    Math.max(0, wait),
  );
}

/** Test-only: forget the follow throttle. */
export function resetFollowThrottle(): void {
  if (pendingTimer) clearTimeout(pendingTimer);
  pendingTimer = null;
  pendingRef = null;
  lastJump = 0;
}
