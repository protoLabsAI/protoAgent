// Keybinding combo normalization + display (ADR 0063). A "combo" is a stable, normalized
// string like "mod+k", "mod+shift+k", "ctrl+tab", "mod+1", "/". `mod` = the platform primary
// (⌘ on mac, Ctrl elsewhere); a literal `ctrl`/`meta` is the OTHER platform modifier.
const IS_MAC =
  typeof navigator !== "undefined" && /Mac|iP(hone|ad|od)/.test(navigator.platform || navigator.userAgent || "");

const MODIFIER_KEYS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock", "Dead"]);

function normalizeKey(key: string): string {
  if (key === " ") return "space";
  return key.toLowerCase(); // "Tab"→"tab", "Enter"→"enter", "ArrowUp"→"arrowup", "A"→"a", "1"→"1"
}

/** A KeyboardEvent → normalized combo. Returns "" for a bare modifier press (so holding ⌘
 *  alone never matches). `mod` is the platform primary; the secondary platform mod is kept
 *  distinctly so a fork can bind it. */
export function eventToCombo(e: KeyboardEvent): string {
  if (MODIFIER_KEYS.has(e.key)) return "";
  const parts: string[] = [];
  const primary = IS_MAC ? e.metaKey : e.ctrlKey;
  const secondary = IS_MAC ? e.ctrlKey : e.metaKey;
  if (primary) parts.push("mod");
  if (secondary) parts.push(IS_MAC ? "ctrl" : "meta");
  if (e.altKey) parts.push("alt");
  if (e.shiftKey) parts.push("shift");
  parts.push(normalizeKey(e.key));
  return parts.join("+");
}

const SEGMENT_LABEL: Record<string, string> = {
  mod: IS_MAC ? "⌘" : "Ctrl",
  ctrl: IS_MAC ? "⌃" : "Ctrl",
  meta: IS_MAC ? "⌘" : "Win",
  alt: IS_MAC ? "⌥" : "Alt",
  shift: IS_MAC ? "⇧" : "Shift",
  tab: "Tab",
  enter: "Enter",
  escape: "Esc",
  space: "Space",
  arrowup: "↑",
  arrowdown: "↓",
  arrowleft: "←",
  arrowright: "→",
};

/** Human display for a combo, e.g. "mod+shift+k" → "⌘⇧K" (mac) / "Ctrl+Shift+K" (else). */
export function formatCombo(combo: string): string {
  if (!combo) return "—";
  const labels = combo.split("+").map((p) => SEGMENT_LABEL[p] ?? (p.length === 1 ? p.toUpperCase() : p));
  return labels.join(IS_MAC ? "" : "+");
}

/** True when focus is in a text-editing context (so plain-key bindings don't fire while typing). */
export function isEditableTarget(target: EventTarget | null): boolean {
  const el = target instanceof HTMLElement ? target : null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}
