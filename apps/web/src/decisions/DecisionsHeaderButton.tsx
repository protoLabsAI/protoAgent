import { useLayoutEffect, useRef, type RefObject } from "react";
import { MessageCircleQuestion } from "lucide-react";
import { Button, Count } from "@protolabsai/ui/primitives";

import { DecisionsSheet } from "./DecisionsPanel";
import { decisionsLabel } from "./DecisionsWidget";
import { useDecisions } from "./useDecisions";

/**
 * The phone entry point (#4725, §2.2): one header button beside New chat, driven by the fleet-wide
 * {@link useDecisions} store. It renders only while something is pending after the first load, so
 * the header never carries an empty slot and never flashes "0" (§5.2). Tapping it opens
 * {@link DecisionsSheet} (a bottom `Drawer`). Not mounted here — the mount card places it in
 * `MobileShell` before New chat (chat root) and in the spacer slot (pushed views).
 *
 * It is an ACTION button — a label (the `MessageCircleQuestion` glyph plus a `Count`, both
 * `aria-hidden`, with the whole name spoken from `aria-label`) that does one thing. Josh's rule
 * and revision 1 §2.2 (Q23, Q24) require the DS `Button` for that, so it ships as a ghost icon
 * `Button`: the DS owns its border/background, the `:focus-visible` ring and the touch-target
 * floor (the `pl-btn--icon` ::after overlay reaches 44px under `@media (hover: none)`). No
 * chrome or ring is hand-rolled here. The icon↔count gap and the `flex: 0 0 auto` header LAYOUT
 * belong to the mount card's `mobile-shell.css`, not this component.
 */

export function DecisionsHeaderButton({
  fallbackFocus,
}: {
  /** Where focus lands on close when the button itself has gone (nothing left to wait on) — the
   *  New chat button (§2.2, §5.1). */
  fallbackFocus?: RefObject<HTMLElement | null>;
}) {
  const { decisions, pendingCount, loaded, panelOpen, openPanel, closePanel, answer } =
    useDecisions();
  // The DS `Button` is a plain function component and does not forward a ref (React 19 drops ref
  // only through props, which `ButtonProps` does not declare), so hold the ref on a transparent
  // wrapper and reach the `<button>` through it — the same idiom as DirectiveEntry.
  const slotRef = useRef<HTMLSpanElement>(null);
  const wasOpen = useRef(false);

  // The DS Drawer traps focus but never returns it to the opener on close, so do it here: to the
  // button if it is still mounted, else to New chat — the button unmounts when the last decision
  // clears while the sheet is open (§5.1).
  useLayoutEffect(() => {
    if (wasOpen.current && !panelOpen) {
      const btn = slotRef.current?.querySelector("button");
      if (btn && document.contains(btn)) btn.focus();
      else fallbackFocus?.current?.focus();
    }
    wasOpen.current = panelOpen;
  }, [panelOpen, fallbackFocus]);

  const shown = loaded && pendingCount > 0;
  return (
    <>
      {shown ? (
        <span ref={slotRef}>
          <Button
            variant="ghost"
            size="md"
            icon
            type="button"
            aria-label={decisionsLabel(pendingCount)}
            title="Decisions waiting on you"
            aria-haspopup="dialog"
            aria-expanded={panelOpen}
            onClick={openPanel}
          >
            <MessageCircleQuestion size={20} aria-hidden />
            <Count aria-hidden>{pendingCount > 9 ? "9+" : pendingCount}</Count>
          </Button>
        </span>
      ) : null}
      {/* Rendered whenever the panel is open — even after the button has gone — so it can show
          "That's everything" and return focus to New chat on close (§5.1). */}
      <DecisionsSheet
        open={panelOpen}
        onClose={closePanel}
        decisions={decisions}
        loading={!loaded}
        onAnswer={answer}
      />
    </>
  );
}
