import { useEffect } from "react";

import { registeredKeybindings } from "../ext/keybindingRegistry";
import { eventToCombo, isEditableTarget } from "./combo";
import { useKbIntents } from "./intents";
import { resolveBinding } from "./resolve";

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
      const hit = resolveBinding(registeredKeybindings(), combo, {
        scopes: focusedScopes(e.target),
        editable: isEditableTarget(e.target),
      });
      if (!hit) return;
      e.preventDefault();
      try {
        hit.run(e);
      } catch {
        /* a binding action must never break key handling */
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}

// Run a chord forwarded OUT of a sandboxed plugin iframe (#1457).
//
// Keys pressed inside a plugin view never reach the listener above — the iframe is a
// separate document — so every host shortcut was dead while a plugin view had focus. The
// kit forwards chords it didn't handle itself and the host resolves them here, through
// the SAME `resolveBinding` the DOM path uses, so precedence and the typing gate behave
// identically wherever focus happens to be.
//
// Only GLOBAL bindings are eligible: the focus chain lives inside the iframe, so the host
// genuinely cannot know which of its panels is focused, and firing another panel's scoped
// chord from a plugin view would be worse than not firing at all. `editable` is the
// page's claim that focus is in one of its own inputs.
//
// Returns true when a binding ran, so the caller can tell the page whether the chord was
// consumed.
export function runForwardedCombo(combo: string, editable = false): boolean {
  const hit = resolveBinding(registeredKeybindings(), combo, { scopes: new Set(), editable });
  if (!hit) return false;
  try {
    // A forwarded chord has no host KeyboardEvent behind it. Bindings receive the event
    // only to inspect/stop it, so a minimal synthetic one keeps the signature honest
    // without pretending the key was pressed in this document.
    hit.run(new KeyboardEvent("keydown"));
  } catch {
    /* a binding action must never break key handling */
  }
  return true;
}
