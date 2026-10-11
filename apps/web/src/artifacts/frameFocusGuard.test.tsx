// Focus theft (#4076): a hosted frame that focuses itself must not take the composer's focus;
// the operator's own click or Tab into it still works.
import { act, createElement as h, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetFrameFocusGuardForTest, useFrameFocusGuard } from "./frameFocusGuard";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

function Host() {
  const ref = useRef<HTMLIFrameElement | null>(null);
  useFrameFocusGuard(ref, true);
  return h("div", null, h("textarea", { "data-testid": "composer" }), h("iframe", { ref, "data-testid": "frame" }));
}

const composer = () => container.querySelector<HTMLTextAreaElement>('[data-testid="composer"]')!;
const frame = () => container.querySelector<HTMLIFrameElement>('[data-testid="frame"]')!;

/** Focus moves into the frame: the frame becomes activeElement and this window blurs. */
function focusIntoFrame() {
  frame().focus();
  window.dispatchEvent(new Event("blur"));
  act(() => {
    vi.runAllTimers();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  resetFrameFocusGuardForTest();
  // A jsdom without the API: pin it so hover-plus-activation is exercised deterministically.
  Object.defineProperty(navigator, "userActivation", { configurable: true, value: { isActive: false } });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(h(Host)));
  composer().focus();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
});

describe("useFrameFocusGuard", () => {
  it("a self-focusing frame never keeps the composer's focus", () => {
    expect(document.activeElement).toBe(composer());
    focusIntoFrame();
    expect(document.activeElement).toBe(composer());
  });

  it("a hover alone (no press) does not license a self-focus", () => {
    frame().dispatchEvent(new Event("pointerenter"));
    focusIntoFrame();
    expect(document.activeElement).toBe(composer());
  });

  it("a click into the frame (pointer over it + user activation) still focuses it", () => {
    frame().dispatchEvent(new Event("pointerenter"));
    Object.defineProperty(navigator, "userActivation", { configurable: true, value: { isActive: true } });
    focusIntoFrame();
    expect(document.activeElement).toBe(frame());
  });

  it("Tab into the frame still works", () => {
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    focusIntoFrame();
    expect(document.activeElement).toBe(frame());
  });
});
