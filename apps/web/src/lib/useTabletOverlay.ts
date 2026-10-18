import { useCallback, useEffect, useRef, useState } from "react";

import { useUI } from "../state/uiStore";
import { TABLET_PORTRAIT_QUERY } from "./autoOpen";

// Tablet portrait (#4336 S5). Between 768px and 1024px a touch tablet held upright gets the
// desktop dual-rail shell (the mobile shell stops at 767px, ADR 0086) — two 64px rails plus a
// 360px right column leave chat at 275–341px, narrower than a phone. Rather than a third shell,
// the right column turns into an OVERLAY in that band: chat keeps the full stage, and the right
// panel floats over it when opened, over a scrim that dismisses it on a tap.
//
// Gated on INPUT, not just width: a narrow desktop window, or an iPad with a trackpad attached
// (hover: hover / pointer: fine), keeps the split. Landscape keeps the split too — at 1024px+
// the chat column is already wider than 530px.
// The query lives with the auto-open gate (lib/autoOpen.ts) so both agree on what "tablet
// portrait" is.
export { TABLET_PORTRAIT_QUERY };

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
 * Drives the portrait overlay. Returns `active` (the right column renders as an overlay) and
 * `dismiss` (what the scrim over chat calls on a tap).
 *
 *  - Entering the band collapses the right panel once (an open overlay on load would cover
 *    chat), and leaving it restores the panel if this hook was the one that collapsed it — so
 *    rotating to landscape gives back the split the operator had.
 *  - While the overlay is open a scrim covers chat (App renders it); tapping it dismisses.
 *  - Opens the operator asks for (rail icon, palette, a tapped chip) work as on desktop: they
 *    un-collapse the panel through the store, which shows the overlay. Opens they did NOT ask
 *    for are suppressed here as on a phone — see lib/autoOpen.ts.
 *
 * Off when chat itself is not on the left dock — overlaying the right column would then cover
 * chat with chat's own sibling and the split is the lesser evil.
 */
export function useTabletOverlay(opts: { isMobile: boolean; chatOnLeft: boolean }): {
  active: boolean;
  dismiss: () => void;
} {
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

  const dismiss = useCallback(() => {
    // The operator closed it themselves — don't re-open it on rotate.
    autoCollapsed.current = false;
    useUI.getState().setRightCollapsed(true);
  }, []);

  return { active, dismiss };
}
