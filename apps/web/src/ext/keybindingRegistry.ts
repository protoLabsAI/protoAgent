// Keybinding registry (ADR 0063) — the fork/plugin seam, mirroring the other src/ext/
// registries (slash / composer / palette). A binding maps a normalized combo to an action,
// optionally scoped to a focused panel. Core defaults register through this SAME seam
// (see src/keybindings/coreKeybindings.ts) — no core bypass.
//
// Last-write-wins by id (HMR-safe: a module re-eval re-registers the same id and replaces
// it). `register*` returns an unregister fn for component-scoped registration.

export type Keybinding = {
  /** Stable id (e.g. "chat.new"); the key for user overrides + dedup. */
  id: string;
  /** Human label for the Keyboard-shortcuts settings UI. */
  label: string;
  /** Settings grouping (e.g. "General", "Chat"). */
  group?: string;
  /** Default combo, normalized (e.g. "mod+k", "mod+shift+k", "ctrl+tab", "/"). */
  defaultKeys: string;
  /** Focus scope: undefined = global (fires anywhere); else a `data-kb-scope` id, so the
   *  binding fires only when focus is within that panel/view (e.g. "chat"). */
  scope?: string;
  /** Fire even when focus is in an editable field (default false). Mod-combos that should
   *  work while typing (e.g. ⌃Tab, ⌘1) set this true. */
  allowInInput?: boolean;
  /** What the binding does. Receives the raw event (already preventDefault'd by the host). */
  run: (e: KeyboardEvent) => void;
};

const _bindings = new Map<string, Keybinding>();

export function registerKeybinding(binding: Keybinding): () => void {
  _bindings.set(binding.id, binding);
  return () => {
    if (_bindings.get(binding.id) === binding) _bindings.delete(binding.id);
  };
}

export function registeredKeybindings(): Keybinding[] {
  return [..._bindings.values()];
}
