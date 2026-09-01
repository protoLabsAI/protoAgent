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

// Run ONE binding's action by id — the command palette's path (ADR 0061): a ⌘K row that
// advertises a shortcut has to run the action that shortcut runs, or the row is a lie.
//
// By id and not through `resolveBinding`, because the palette names an ACTION, not a chord:
// two bindings can share a combo, and which one a chord resolves to depends on focus. That
// makes the two things `resolveBinding` enforces the caller's problem, and only one of them
// is a real hazard:
//   • `scope` — resolve.ts is its ONLY enforcement point, and this bypasses it. The palette
//     overlay is never inside `[data-kb-scope="chat"]` (ChatSurface.tsx is the one element
//     that declares it), so a chat-scoped action invoked from a ⌘K row would fire with the
//     chat surface possibly not even on screen. The palette does not bypass the check, it
//     satisfies it: the row carries the surface its binding's scope names — or, for a GLOBAL
//     binding whose action still needs a surface mounted, one it names itself — and
//     `applyNavIntent` opens that surface first, FLUSHING the render so the surface is in the
//     DOM and not merely in the store, which is what an action that walks the DOM needs
//     (app/palette/nav.ts, NavIntent kind "keybinding"). A caller that can't do that must
//     not call this for a scoped binding.
//   • `allowInInput` — not a hazard. It exists so a plain key (`/`) doesn't fire while the
//     operator is typing it into a field; choosing a row off a list is not a stray keystroke.
//
// The synthetic event mirrors `runForwardedCombo` above, for the same reason: `run` is typed
// `(e: KeyboardEvent) => void`, no core binding reads the event, and inventing a plausible
// "real" event would pretend a key was pressed. The try/catch mirrors the keydown host — a
// throwing binding must not take the palette down with it.
//
// Returns false when nothing is registered under `id` (a stale row is then a no-op, not a
// crash), true when a binding was found and invoked.
export function runBindingById(id: string): boolean {
  const binding = registeredKeybindings().find((b) => b.id === id);
  if (!binding) return false;
  try {
    binding.run(new KeyboardEvent("keydown"));
  } catch {
    /* a binding action must never break its caller */
  }
  return true;
}
