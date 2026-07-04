// Shared logic for the chat tab-strip's "Shift held" visual cues. While Shift is down the DS
// TabBar shows two affordances: the add "+" becomes the incognito EyeOff (Shift+click opens a
// new incognito chat, #1697/#1744) and each tab's ✕ becomes a red trashcan on hover (Shift+click
// = quick-delete, #1373). Both ride one "is Shift held" signal (trackShiftHeld → the
// `--incognito`/`--del` wrapper classes → CSS); the incognito gesture itself is decided by
// isIncognitoAddClick. Kept here as pure functions so the handler and its unit test pin the
// exact same behavior.

// The DS TabBar's add "+" button (@protolabsai/ui navigation TabBar).
export const ADD_SELECTOR = ".pl-tabbar__add";

// Shift+click on the add "+" opens a NEW incognito chat (#1697) — the click-path twin of the
// tab context menu's "New incognito chat". True only when Shift is held AND the click landed on
// (or inside) the add button.
export function isIncognitoAddClick(target: EventTarget | null, shiftKey: boolean): boolean {
  if (!shiftKey) return false;
  const el = target as (Element & { closest(sel: string): Element | null }) | null;
  return !!el?.closest?.(ADD_SELECTOR);
}

// Track whether Shift is currently held, calling `onChange` on every transition. keyup updates
// from the event's own modifier state, and a window blur clears it (a blur mid-hold would
// otherwise strand the cue "on" — the keyup fires on a window we no longer have focus for).
// Returns a cleanup that detaches every listener; call it on unmount.
export function trackShiftHeld(onChange: (held: boolean) => void): () => void {
  const sync = (e: KeyboardEvent) => onChange(e.shiftKey);
  const clear = () => onChange(false);
  window.addEventListener("keydown", sync);
  window.addEventListener("keyup", sync);
  window.addEventListener("blur", clear);
  return () => {
    window.removeEventListener("keydown", sync);
    window.removeEventListener("keyup", sync);
    window.removeEventListener("blur", clear);
  };
}
