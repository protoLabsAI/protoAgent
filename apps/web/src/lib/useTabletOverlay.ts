import { useEffect, useRef, useState } from "react";

import { useUI } from "../state/uiStore";

// Tablet portrait (#4336 S5). Between 768px and 1024px a touch tablet held upright gets the
// desktop dual-rail shell (the mobile shell stops at 767px, ADR 0086) — two 64px rails plus a
// 360px right column leave chat at 275–341px, narrower than a phone. Rather than a third shell,
// the right column turns into an OVERLAY in that band: chat keeps the full stage, and the right
// panel floats over it when opened and is dismissed by tapping back into chat.
//
// Gated on INPUT, not just width: a narrow desktop window, or an iPad with a trackpad attached
// (hover: hover / pointer: fine), keeps the split. Landscape keeps the split too — at 1024px+
// the chat column is already wider than 530px.
export const TABLET_PORTRAIT_QUERY =
  "(min-width: 768px) and (max-width: 1024px) and (orientation: portrait) and (hover: none) and (pointer: coarse)";

function useMedia(query: string): boolean {
  const [match, setMatch] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setMatch(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return match;
}

/**
 * True while the right column renders as an overlay over chat. Drives three behaviours:
 *
 *  - Entering the band collapses the right panel once (an open overlay on load would cover
 *    chat), and leaving it restores the panel if this hook was the one that collapsed it — so
 *    rotating to landscape gives back the split the operator had.
 *  - A tap inside the left column (chat) while the overlay is open dismisses it — the
 *    light-dismiss every overlay on a touch device is expected to have.
 *  - Programmatic opens (palette, artifact/code chips) still work: they un-collapse the panel
 *    through the store, which simply shows the overlay.
 *
 * Off when chat itself is not on the left dock — overlaying the right column would then cover
 * chat with chat's own sibling and the split is the lesser evil.
 */
export function useTabletOverlay(opts: { isMobile: boolean; chatOnLeft: boolean }): boolean {
  const portrait = useMedia(TABLET_PORTRAIT_QUERY);
  const leftCollapsed = useUI((s) => s.leftCollapsed);
  const active = portrait && !opts.isMobile && opts.chatOnLeft && !leftCollapsed;
  const autoCollapsed = useRef(false);

  useEffect(() => {
    const { rightCollapsed, setRightCollapsed } = useUI.getState();
    if (active) {
      if (!rightCollapsed) {
        autoCollapsed.current = true;
        setRightCollapsed(true);
      }
    } else if (autoCollapsed.current) {
      autoCollapsed.current = false;
      setRightCollapsed(false);
    }
  }, [active]);

  useEffect(() => {
    if (!active) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Element | null;
      if (!t?.closest?.(".pl-appshell__col--left")) return;
      const { rightCollapsed, setRightCollapsed } = useUI.getState();
      if (rightCollapsed) return;
      // The operator closed it themselves — don't re-open it on rotate.
      autoCollapsed.current = false;
      setRightCollapsed(true);
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [active]);

  return active;
}
