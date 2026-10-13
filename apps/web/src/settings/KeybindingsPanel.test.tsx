// The per-binding "reset to default" control in Settings ▸ Keyboard is now a DS
// `<Button variant="ghost" size="xs" icon>` rendering a lucide `RotateCcw` glyph, not a
// hand-rolled `<button className="kb-reset">↺</button>` (protoContent#551, card 4c). This
// suite guards the swap: the reset control must stay a real button that keeps its title,
// aria-label and reset handler; the `kb-key` combo recorders must stay raw (they capture
// keys, they aren't actions); and neither the `.kb-reset` class nor the ↺ glyph may return.
// createRoot/act + the real DS components, like the other console UI suites
// (UtilityWidget.test.tsx) — no testing-library dep.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { registerKeybinding } from "../ext/keybindingRegistry";
import { useKeybindingOverrides } from "../keybindings/overrides";
import { KeybindingsPanel } from "./KeybindingsPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;
let unregister: () => void;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  // One registered binding in a known group, so the panel renders exactly one row.
  unregister = registerKeybinding({
    id: "test.action",
    label: "Test Action",
    group: "General",
    defaultKeys: "mod+t",
    run: () => {},
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  unregister();
  useKeybindingOverrides.getState().resetAll();
});

/** The reset control for the seeded binding, by its stable aria-label. */
const resetBtn = () =>
  document.querySelector<HTMLButtonElement>('[aria-label="Reset Test Action to default"]');

describe("KeybindingsPanel — the DS-Button reset control (protoContent#551)", () => {
  it("renders the reset control as a DS Button with a lucide RotateCcw glyph", async () => {
    // A row only shows a reset control once it carries an override.
    useKeybindingOverrides.getState().setBinding("test.action", "mod+shift+t");
    await act(async () => {
      root.render(h(KeybindingsPanel));
    });

    const btn = resetBtn();
    expect(btn).not.toBeNull();
    expect(btn!.tagName).toBe("BUTTON");
    // The whole point of the card: it's a DS Button (carries `pl-btn`), not a hand-rolled
    // `.kb-reset` button, and it no longer renders the ↺ glyph.
    expect(btn!.classList.contains("pl-btn")).toBe(true);
    expect(btn!.classList.contains("kb-reset")).toBe(false);
    expect(btn!.textContent).not.toContain("↺");
    // Title + aria-label are unchanged from the hand-rolled control.
    expect(btn!.getAttribute("title")).toBe("Reset to default");
    // The glyph is the lucide RotateCcw SVG, marked aria-hidden so the button reads by label.
    const svg = btn!.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg!.getAttribute("aria-hidden")).toBe("true");
  });

  it("clicking the reset control clears the override (handler unchanged)", async () => {
    useKeybindingOverrides.getState().setBinding("test.action", "mod+shift+t");
    await act(async () => {
      root.render(h(KeybindingsPanel));
    });
    expect(useKeybindingOverrides.getState().overrides["test.action"]).toBe("mod+shift+t");

    act(() => resetBtn()!.click());

    expect(useKeybindingOverrides.getState().overrides["test.action"]).toBeUndefined();
    // With the override gone the control unmounts — the row is back to its default combo.
    expect(resetBtn()).toBeNull();
  });

  it("leaves the kb-key combo recorder raw and drops the .kb-reset class entirely", async () => {
    useKeybindingOverrides.getState().setBinding("test.action", "mod+shift+t");
    await act(async () => {
      root.render(h(KeybindingsPanel));
    });

    // The combo recorder is a key-capture control, not an action — it stays a raw kb-key button.
    const recorder = document.querySelector<HTMLButtonElement>(".kb-key");
    expect(recorder).not.toBeNull();
    expect(recorder!.tagName).toBe("BUTTON");
    // `.kb-reset` must be gone from the rendered tree (and its CSS).
    expect(document.querySelector(".kb-reset")).toBeNull();
  });
});
