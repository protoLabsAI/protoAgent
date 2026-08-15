// The one-time "this runs code" consent (ADR 0071 D3, #2721) — the confirm carries
// the checkbox state, and cancel never confirms. jsdom + createElement (no JSX in
// the .test.ts unit harness), same pattern as the NewAgentPanel tests.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TrustAckDialog } from "./TrustAckDialog";

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

function mount(onConfirm = vi.fn(), onClose = vi.fn()) {
  act(() => {
    root.render(h(TrustAckDialog, { source: "github.com/rando/thing", onConfirm, onClose }));
  });
  return { onConfirm, onClose };
}

function button(re: RegExp): HTMLButtonElement {
  const btn = [...document.body.querySelectorAll("button")].find((b) => re.test(b.textContent ?? ""));
  if (!btn) throw new Error(`no button matching ${re}`);
  return btn;
}

describe("TrustAckDialog", () => {
  it("names the source and confirms with trustAll=false by default", () => {
    const { onConfirm } = mount();
    expect(document.body.textContent).toContain("github.com/rando/thing");
    expect(document.body.textContent).toContain("runs code");
    act(() => button(/Trust and install/).click());
    expect(onConfirm).toHaveBeenCalledWith(false);
  });

  it("carries the don't-ask-again checkbox into the confirm", () => {
    const { onConfirm } = mount();
    const box = document.body.querySelector<HTMLInputElement>('input[type="checkbox"]');
    expect(box).not.toBeNull();
    act(() => box!.click());
    act(() => button(/Trust and install/).click());
    expect(onConfirm).toHaveBeenCalledWith(true);
  });

  it("cancel closes without confirming", () => {
    const { onConfirm, onClose } = mount();
    act(() => button(/Cancel/).click());
    expect(onClose).toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
