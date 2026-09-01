// ADR 0057 — the palette's only VISIBLE way in.
//
// Everything the ⌘K arc built — ranking, recents, every Settings section, the chat's verbs,
// the keyboard actions, live knowledge search — was reachable by exactly one route: the
// `palette.toggle` chord. `grep -rn "togglePalette" apps/web/src` returned a single call site,
// the binding itself. An operator who never read the guide never learned the palette existed,
// and none of it was discoverable from the console it lives in.
//
// So the affordance's job is not "another button". It is to TEACH the chord: it looks like the
// search field it opens, and its only text is the combo, so the thing you click is also the
// thing that tells you how to stop clicking it.
//
// ── Two details that are load-bearing ──────────────────────────────────────────────────
//
// The combo is READ FROM THE BINDING, never written as a literal. `palette.toggle` is
// rebindable in Settings ▸ Keyboard (`protoagent.keybindings`, a global override), so a
// hard-coded "⌘⇧K" starts lying the moment an operator rebinds it — and lying about a
// shortcut is worse than omitting one, because it teaches a chord that does nothing. Subscribed
// through `useKeybindingOverrides` rather than read once, so a rebind re-labels it live.
//
// The visible text is the COMBO ONLY — the words "Command palette" appear as `aria-label` and
// tooltip, never as a text node. That is deliberate and it is not merely stylistic: that exact
// string is the `palette.toggle` binding's label, which Settings ▸ Keyboard also renders, and
// the settings dialog PORTALS over the shell rather than unmounting it. A second visible
// "Command palette" text node would make `getByText("Command palette", { exact: true })`
// resolve to two elements — a Playwright strict-mode violation in a spec that has nothing to
// do with this button (`e2e/keybindings.spec.ts`). Attributes are not matched by `getByText`,
// so the label lives there and the trap never arms.
import { Search } from "lucide-react";

// The button styles live beside the root view they belong with; imported here too rather
// than relying on rootView.tsx having been pulled in first (CSS imports dedupe).
import "./palette.css";

import { Tooltip } from "@protolabsai/ui/overlays";

import { registeredKeybindings } from "../ext/keybindingRegistry";
import { useKbIntents } from "../keybindings/intents";
import { formatCombo } from "../keybindings/combo";
import { useKeybindingOverrides } from "../keybindings/overrides";

/** The binding this button advertises AND invokes — one id, so the two can't drift. */
export const PALETTE_BINDING_ID = "palette.toggle";

/** The live combo for `palette.toggle`, formatted for display, or "" when nothing registered
 *  it. The empty case is real: only `App` imports the keybindings barrel, so in the frameless
 *  desktop launcher — which mounts the palette but not the shell — there is no binding to
 *  read. Rendering no chord there is correct; the chord isn't live in that window either. */
export function paletteCombo(overrides: Record<string, string>): string {
  const binding = registeredKeybindings().find((b) => b.id === PALETTE_BINDING_ID);
  if (!binding) return "";
  return formatCombo(overrides[binding.id] ?? binding.defaultKeys);
}

/** The utility-bar affordance. Shaped like the search field it opens rather than like the
 *  icon pills beside it, because a magnifier that reads "⌘⇧K" says what it does before you
 *  click it — which is the entire point of adding it. */
export function PaletteButton() {
  const overrides = useKeybindingOverrides((s) => s.overrides);
  const toggle = useKbIntents((s) => s.togglePalette);
  const combo = paletteCombo(overrides);
  return (
    <Tooltip label="Search commands, surfaces and agents — the command palette">
      <button
        type="button"
        className="util-btn palette-btn"
        aria-label="Search commands"
        aria-keyshortcuts={combo || undefined}
        data-testid="palette-widget"
        onClick={toggle}
      >
        <Search size={14} />
        {/* The chord is the label. `aria-hidden` because `aria-keyshortcuts` above already
            carries it for assistive tech in a form they announce properly — a screen reader
            reading the glyph string "⌘⇧K" character by character is noise, not help. */}
        {combo ? (
          <span className="palette-btn__combo" aria-hidden>
            {combo}
          </span>
        ) : null}
      </button>
    </Tooltip>
  );
}
