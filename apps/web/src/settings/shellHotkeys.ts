// OS-global (desktop shell) hotkey helpers (#1675) — pure, react-free: the chord
// grammar bridge between DOM KeyboardEvents and the Rust shell's global-hotkey
// parser ("ctrl+alt+space", "super+shift+p"), plus display formatting.

export type ShellHotkey = { id: string; chord: string; registered: boolean; error: string | null };

export const SHELL_HOTKEY_LABELS: Record<string, string> = {
  console_toggle: "Toggle console window",
  quick_launcher: "Quick launcher",
};

/** KeyboardEvent → the global-hotkey chord grammar. Null while only modifiers are
 *  held, for keys the shell can't register, or for a bare key (a modifier-less
 *  chord must never be a SYSTEM-WIDE hotkey — it would eat normal typing). */
export function eventToShellChord(e: KeyboardEvent): string | null {
  const mods: string[] = [];
  if (e.metaKey) mods.push("super");
  if (e.ctrlKey) mods.push("ctrl");
  if (e.altKey) mods.push("alt");
  if (e.shiftKey) mods.push("shift");
  const k = e.key;
  let key: string | null = null;
  if (k === " " || k === "Spacebar") key = "space";
  else if (/^[a-zA-Z0-9]$/.test(k)) key = k.toLowerCase();
  else if (/^F([1-9]|1[0-9]|2[0-4])$/.test(k)) key = k.toLowerCase();
  if (!key || mods.length === 0) return null;
  return [...mods, key].join("+");
}

export function formatShellChord(chord: string, platform = navigator.platform): string {
  const mac = platform.toLowerCase().includes("mac");
  return chord
    .split("+")
    .map((part) => {
      if (part === "super") return mac ? "⌘" : "Win";
      if (part === "ctrl") return mac ? "⌃" : "Ctrl";
      if (part === "alt") return mac ? "⌥" : "Alt";
      if (part === "shift") return mac ? "⇧" : "Shift";
      return part.length === 1 ? part.toUpperCase() : part.charAt(0).toUpperCase() + part.slice(1);
    })
    .join(mac ? "" : "+");
}
