// The one gate for opens the operator did NOT ask for: an artifact the live turn just wrote,
// the agent's show_code, code-pane follow mode. ADR 0086: on a phone those never take over
// the screen — the chip lands in chat and a tap opens the surface.
//
// A touch tablet held upright (#4336 S5) gets the same rule. There the right panel floats
// OVER chat (lib/useTabletOverlay.ts), so an auto-open would cover the conversation mid-turn
// exactly as a pushed surface would on a phone.

/** Below the shell switch: the chat-first phone shell (lockstep with lib/useIsMobile.ts). */
export const MOBILE_QUERY = "(max-width: 767px)";

/** Touch tablet in portrait: the desktop shell with the right column as an overlay. */
export const TABLET_PORTRAIT_QUERY =
  "(min-width: 768px) and (max-width: 1024px) and (orientation: portrait) and (hover: none) and (pointer: coarse)";

function matches(query: string): boolean {
  try {
    return typeof window !== "undefined" && window.matchMedia(query).matches;
  } catch {
    return false;
  }
}

/** True on the phone shell. */
export function isMobileViewport(): boolean {
  return matches(MOBILE_QUERY);
}

/** True where an unrequested open must not bring a surface on screen: a phone, or a touch
 *  tablet in portrait. The open still SEEDS its surface; the chip opens it on a tap. */
export function autoOpenSuppressed(): boolean {
  return isMobileViewport() || matches(TABLET_PORTRAIT_QUERY);
}
