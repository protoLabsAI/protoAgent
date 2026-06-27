import { useEffect } from "react";

import { registeredKeybindings } from "../ext/keybindingRegistry";
import { eventToCombo, isEditableTarget } from "./combo";
import { useKbIntents } from "./intents";
import { effectiveCombo } from "./overrides";

// The focused scope chain: walk up from the event target collecting every `data-kb-scope`
// (a panel/view marks its root, e.g. the chat stage = "chat"). A scoped binding fires only
// when its scope is in this chain; a global binding (no scope) fires anywhere.
function focusedScopes(target: EventTarget | null): Set<string> {
  const scopes = new Set<string>();
  let el = target instanceof Element ? (target as HTMLElement) : null;
  while (el) {
    const s = el.dataset?.kbScope;
    if (s) s.split(/\s+/).forEach((x) => x && scopes.add(x));
    el = el.parentElement;
  }
  return scopes;
}

// The single global keydown host (ADR 0063). Mounted once (App). Resolves the pressed combo
// against the registry — honoring the focused scope + the typing gate + user overrides — and
// runs the most-specific match (a panel-scoped binding beats a global one for the same combo).
export function useGlobalKeybindings(): void {
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      if (useKbIntents.getState().capturing) return; // settings is recording a new shortcut
      const combo = eventToCombo(e);
      if (!combo) return;
      const matches = registeredKeybindings().filter((b) => effectiveCombo(b) === combo);
      if (matches.length === 0) return;

      const editable = isEditableTarget(e.target);
      const scopes = focusedScopes(e.target);
      const eligible = matches.filter(
        (b) => (!b.scope || scopes.has(b.scope)) && (b.allowInInput || !editable),
      );
      if (eligible.length === 0) return;

      // Most-specific wins: a scoped binding (focused in that panel) beats a global one.
      eligible.sort((a, b) => (b.scope ? 1 : 0) - (a.scope ? 1 : 0));
      e.preventDefault();
      try {
        eligible[0].run(e);
      } catch {
        /* a binding action must never break key handling */
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
