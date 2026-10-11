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

  // #4154 review (MAJOR): the operator's own keydown on the page grants transient activation,
  // so typing in the composer with the pointer parked over a frame must not license a self-focus.
  it("typing in the composer with the pointer over the frame never licenses a self-focus", () => {
    frame().dispatchEvent(new Event("pointerenter"));
    composer().dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
    Object.defineProperty(navigator, "userActivation", { configurable: true, value: { isActive: true } });
    focusIntoFrame();
    expect(document.activeElement).toBe(composer());
  });

  it("a page click elsewhere moments ago doesn't license a hover-time self-focus either", () => {
    composer().dispatchEvent(new Event("pointerdown", { bubbles: true }));
    frame().dispatchEvent(new Event("pointerenter"));
    Object.defineProperty(navigator, "userActivation", { configurable: true, value: { isActive: true } });
    focusIntoFrame();
    expect(document.activeElement).toBe(composer());
  });

  it("a click into the frame well after the last page keydown still focuses it", () => {
    composer().dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    frame().dispatchEvent(new Event("pointerenter"));
    Object.defineProperty(navigator, "userActivation", { configurable: true, value: { isActive: true } });
    focusIntoFrame();
    expect(document.activeElement).toBe(frame());
  });

  // #4154 review: a Tab only licenses the frame its traversal actually lands in.
  it("a Tab the composer consumed (preventDefault, slash autocomplete) never licenses a self-focus", () => {
    const consume = (e: KeyboardEvent) => e.preventDefault();
    composer().addEventListener("keydown", consume);
    composer().dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
    composer().removeEventListener("keydown", consume);
    focusIntoFrame();
    expect(document.activeElement).toBe(composer());
  });

  it("a Tab that landed on a page element is spent — a later self-focus is refused", () => {
    const button = document.createElement("button");
    container.appendChild(button);
    composer().dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
    button.focus(); // the Tab's default action: focus moves to the next page element
    focusIntoFrame();
    expect(document.activeElement).toBe(button);
  });

  it("a Tab whose traversal didn't reach a frame by the next task is spent", () => {
    composer().dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
    act(() => {
      vi.advanceTimersByTime(0);
    });
    focusIntoFrame();
    expect(document.activeElement).toBe(composer());
  });

  it("one Tab admits one frame: a second frame self-focusing after it is refused", () => {
    composer().dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
    focusIntoFrame();
    expect(document.activeElement).toBe(frame());
    // The frame (licensed) now drops focus back and self-focuses again, same Tab window.
    composer().focus();
    focusIntoFrame();
    expect(document.activeElement).toBe(composer());
  });
});

// Engines without the UserActivation API (Safari < 16.4, Firefox < 120): hover must never stand
// in for a press (#4154 review). The guard denies hover-time focus there; only Tab or a press the
// page itself sees on the frame element counts.
describe("useFrameFocusGuard without navigator.userActivation", () => {
  beforeEach(() => {
    Reflect.deleteProperty(navigator, "userActivation");
    expect("userActivation" in navigator).toBe(false);
  });

  it("a hover with no click never keeps a self-focus", () => {
    frame().dispatchEvent(new Event("pointerenter"));
    focusIntoFrame();
    expect(document.activeElement).toBe(composer());
  });

  it("a long rest over the frame (pointer still inside) never licenses a later self-focus", () => {
    frame().dispatchEvent(new Event("pointerenter"));
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    focusIntoFrame();
    expect(document.activeElement).toBe(composer());
  });

  it("a real click the page sees (pointerdown on the frame element) still focuses it", () => {
    frame().dispatchEvent(new Event("pointerenter"));
    frame().dispatchEvent(new Event("pointerdown"));
    focusIntoFrame();
    expect(document.activeElement).toBe(frame());
  });

  it("Tab into the frame still works", () => {
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    focusIntoFrame();
    expect(document.activeElement).toBe(frame());
  });
});
