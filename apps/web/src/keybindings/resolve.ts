import type { Keybinding } from "../ext/keybindingRegistry";
import { effectiveCombo } from "./overrides";

// The pure half of keybinding resolution (ADR 0063), extracted from `useKeybindings` so
// the SAME precedence rules serve two callers: the global keydown host, and chords
// forwarded out of a sandboxed plugin iframe (#1457). A second implementation for the
// iframe path would drift — and a chord that behaves differently depending on which pane
// had focus is exactly the bug an operator can't diagnose.

export type ResolveContext = {
  /** `data-kb-scope` ids in the focus chain. A scoped binding fires only when its scope
   *  is here; a global binding fires anywhere. */
  scopes: Set<string>;
  /** Focus is in a text field — bindings must opt in with `allowInInput`. */
  editable: boolean;
};

/** The binding that should run for `combo`, or null. Most-specific wins: a scoped
 *  binding (focused in that panel) beats a global one for the same chord. */
export function resolveBinding(
  bindings: Keybinding[],
  combo: string,
  ctx: ResolveContext,
): Keybinding | null {
  const eligible = bindings.filter(
    (b) =>
      effectiveCombo(b) === combo &&
      (!b.scope || ctx.scopes.has(b.scope)) &&
      (b.allowInInput || !ctx.editable),
  );
  if (eligible.length === 0) return null;
  eligible.sort((a, b) => (b.scope ? 1 : 0) - (a.scope ? 1 : 0));
  return eligible[0];
}
