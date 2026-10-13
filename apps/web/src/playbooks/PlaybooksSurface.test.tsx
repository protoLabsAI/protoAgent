// DS audit card 4: the two hand-rolled `<input type="checkbox">` in the skill form
// became DS `Checkbox` (@protolabsai/ui/forms). This guards the two things a swap can
// silently break: the accessible NAME (each checkbox keeps its aria-label, so the DOM
// input still resolves by it) and the STATE LOGIC (unchecking the slash trigger also
// clears "user only", which requires a slash). SkillForm is a pure controlled component,
// so it renders standalone in jsdom — unlike the Suspense/fetch-driven PlaybooksBody.
import { act, createElement as h, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { EMPTY_DRAFT, SkillForm } from "./PlaybooksSurface";
import playbooksSrc from "./PlaybooksSurface.tsx?raw";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

// A stateful wrapper: SkillForm is controlled, so the harness owns the draft and feeds
// setDraft back in — exactly how PlaybooksBody drives it.
function Harness({ initial }: { initial: typeof EMPTY_DRAFT }) {
  const [draft, setDraft] = useState(initial);
  return h(SkillForm, { draft, setDraft, onSave: () => {}, onCancel: () => {}, saving: false, saveLabel: "Create skill" });
}

function render(initial: typeof EMPTY_DRAFT) {
  act(() => root.render(h(Harness, { initial })));
}

const checkbox = (name: string) =>
  container.querySelector<HTMLInputElement>(`input[type="checkbox"][aria-label="${name}"]`);

describe("SkillForm checkboxes → DS Checkbox", () => {
  it("keeps each checkbox's aria-label accessible name and its visible /slash label", () => {
    render({ ...EMPTY_DRAFT, userFacing: true });
    const slash = checkbox("invokable as a slash command");
    const userOnly = checkbox("hide from the agent — operator slash command only");
    expect(slash).toBeTruthy();
    expect(userOnly).toBeTruthy();
    // Visible label text (with the <code>/slash</code> markup) is preserved alongside the aria-label.
    const slashLabel = slash!.closest(".pl-checkbox");
    expect(slashLabel?.textContent).toContain("Invokable as a");
    expect(slashLabel?.querySelector("code")?.textContent).toBe("/slash");
    expect(userOnly!.closest(".pl-checkbox")?.textContent).toContain("Hide from the agent");
  });

  it("no raw <input type=checkbox> renders — the swap uses the DS component", () => {
    render({ ...EMPTY_DRAFT, userFacing: true, userOnly: true });
    // Every checkbox in the tree carries the DS class, i.e. none is a hand-rolled bare input.
    for (const box of container.querySelectorAll('input[type="checkbox"]')) {
      expect(box.classList.contains("pl-checkbox__input")).toBe(true);
    }
    expect(playbooksSrc).not.toContain('type="checkbox"');
  });

  it("unchecking the slash trigger clears user-only", () => {
    render({ ...EMPTY_DRAFT, userFacing: true, userOnly: true });
    expect(checkbox("hide from the agent — operator slash command only")?.checked).toBe(true);

    // Uncheck "invokable as a slash command" → userFacing false, so the user-only row unmounts…
    act(() => checkbox("invokable as a slash command")!.click());
    expect(checkbox("hide from the agent — operator slash command only")).toBeNull();

    // …and re-checking the slash trigger brings user-only back UNCHECKED, proving it was cleared.
    act(() => checkbox("invokable as a slash command")!.click());
    expect(checkbox("hide from the agent — operator slash command only")?.checked).toBe(false);
  });
});
