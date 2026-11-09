import { useLayoutEffect, useRef, type RefObject } from "react";
import { MessageCircleQuestion } from "lucide-react";
import { Count } from "@protolabsai/ui/primitives";

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
 * It is a COMPOSITE trigger — the `MessageCircleQuestion` glyph and a `Count` beside it, both
 * `aria-hidden`, with the whole name spoken from `aria-label` (§6). Josh's rule allows a raw
 * `<button>` for a composite trigger provided it carries the DS focus ring and tokens, so the ring
 * is a token-only `:focus-visible` rule (below) rather than relying on a DS `Button`'s chrome. The
 * mount card adds the header LAYOUT (`flex: 0 0 auto`, the icon↔count gap) to `mobile-shell.css`;
 * this ships the ring so the unmounted button is still keyboard-accessible on its own.
 */

// Token-only ring + the composite layout, so the button is self-contained before it is mounted.
// `:focus-visible` can't be an inline style, so it rides a tiny scoped rule on a dedicated class
// (never a `.pl-*` class). No accent, no pulse — a pending decision is a request, not an alarm.
const HEAD_BTN_CSS = `
.decisions-head-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--pl-space-1);
  flex: 0 0 auto;
  min-width: 44px;
  min-height: 44px;
  padding: 0 var(--pl-space-2);
  border: none;
  border-radius: var(--pl-radius);
  background: none;
  color: var(--pl-color-fg);
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
  touch-action: manipulation;
}
.decisions-head-btn:focus-visible {
  outline: var(--pl-border-width) solid var(--pl-color-fg);
  outline-offset: 2px;
}
`;

export function DecisionsHeaderButton({
  fallbackFocus,
}: {
  /** Where focus lands on close when the button itself has gone (nothing left to wait on) — the
   *  New chat button (§2.2, §5.1). */
  fallbackFocus?: RefObject<HTMLElement | null>;
}) {
  const { decisions, pendingCount, loaded, panelOpen, openPanel, closePanel, answer } =
    useDecisions();
  const btnRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);

  // The DS Drawer traps focus but never returns it to the opener on close, so do it here: to the
  // button if it is still mounted, else to New chat — the button unmounts when the last decision
  // clears while the sheet is open (§5.1).
  useLayoutEffect(() => {
    if (wasOpen.current && !panelOpen) {
      const btn = btnRef.current;
      if (btn && document.contains(btn)) btn.focus();
      else fallbackFocus?.current?.focus();
    }
    wasOpen.current = panelOpen;
  }, [panelOpen, fallbackFocus]);

  const shown = loaded && pendingCount > 0;
  return (
    <>
      {shown ? (
        <>
          <style>{HEAD_BTN_CSS}</style>
          <button
            ref={btnRef}
            type="button"
            className="mshell-head-btn decisions-head-btn"
            aria-label={decisionsLabel(pendingCount)}
            title="Decisions waiting on you"
            aria-haspopup="dialog"
            aria-expanded={panelOpen}
            onClick={openPanel}
          >
            <MessageCircleQuestion size={20} aria-hidden />
            <Count aria-hidden>{pendingCount > 9 ? "9+" : pendingCount}</Count>
          </button>
        </>
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
