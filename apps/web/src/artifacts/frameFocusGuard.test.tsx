// Focus theft (#4076, #4154): a hosted frame is click-to-activate; one that takes keyboard focus
// on its own is handed focus back and quarantined. The operator's own click or Tab still works.
import { act, createElement as h, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetFrameFocusGuardForTest, TAB_INTENT_MS, useFrameFocusGuard } from "./frameFocusGuard";
import { FrameActivationShield, FrameQuarantineNotice } from "./FrameGuardViews";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

function Framed({ frameKey, testId }: { frameKey: string; testId: string }) {
  const ref = useRef<HTMLIFrameElement | null>(null);
  const guard = useFrameFocusGuard(ref, frameKey, true);
  if (guard.quarantined) return h(FrameQuarantineNotice, { what: "artifact", onRun: guard.run });
  return h(
    "div",
    { className: "frame-guard", "data-testid": `${testId}-wrap`, "data-active": guard.active ? "true" : undefined },
    h("iframe", { ref, "data-testid": testId, src: "about:srcdoc" }),
    h(FrameActivationShield, { guard }),
  );
}

function Host({ show = true, frameKey = "k1" }: { show?: boolean; frameKey?: string }) {
  return h(
    "div",
    null,
    h("textarea", { "data-testid": "composer" }),
    h("button", { "data-testid": "page-button" }, "page"),
    show ? h(Framed, { frameKey, testId: "frame" }) : null,
    h(Framed, { frameKey: "k2", testId: "frame2" }),
  );
}

const q = <T extends Element>(id: string) => container.querySelector<T>(`[data-testid="${id}"]`);
const composer = () => q<HTMLTextAreaElement>("composer")!;
const frame = () => q<HTMLIFrameElement>("frame");
const frame2 = () => q<HTMLIFrameElement>("frame2")!;
const shield = () => q<HTMLElement>("frame-wrap")?.querySelector<HTMLElement>('[data-testid="frame-guard-shield"]') ?? null;

/** Focus moves into `f` (its own focus() or a traversal): `f` becomes activeElement and this
 *  window blurs — what every engine reports, synchronously, at the window blur. */
function focusInto(f: HTMLIFrameElement) {
  act(() => {
    f.focus();
    window.dispatchEvent(new Event("blur"));
  });
  // The hand-back runs one task later (see returnFocus).
  act(() => {
    vi.advanceTimersByTime(0);
  });
}

function pressTab(target: Element = composer(), consumed = false) {
  const consume = (e: Event) => e.preventDefault();
  if (consumed) target.addEventListener("keydown", consume);
  target.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
  if (consumed) target.removeEventListener("keydown", consume);
}

function render(props: { show?: boolean; frameKey?: string } = {}) {
  act(() => root.render(h(Host, props)));
}

beforeEach(() => {
  vi.useFakeTimers();
  resetFrameFocusGuardForTest();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  render();
  composer().focus();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
});

describe("useFrameFocusGuard — quarantine", () => {
  it("a frame that focuses itself is blanked in the same task, then handed back and quarantined", () => {
    const f = frame()!;
    act(() => {
      f.focus();
      window.dispatchEvent(new Event("blur"));
      expect(f.getAttribute("src")).toBe("about:blank"); // before any timer runs
    });
    act(() => {
      vi.advanceTimersByTime(0);
    });
    expect(document.activeElement).toBe(composer());
    expect(f.getAttribute("src")).toBe("about:blank"); // its scripts are stopped in the same task
    expect(frame()).toBeNull();
    expect(q("frame-quarantine")?.textContent).toContain("This artifact tried to take keyboard focus.");
  });

  it("a frame that keeps re-focusing never keeps focus (a 1 ms loop is caught on its first steal)", () => {
    const f = frame()!;
    focusInto(f);
    // The loop fires again before the blank navigation commits — still handed straight back.
    focusInto(f);
    expect(document.activeElement).toBe(composer());
    expect(container.querySelectorAll('[data-testid="frame-quarantine"]')).toHaveLength(1);
  });

  it("the quarantine survives a re-render of the same message, and only that frame's", () => {
    focusInto(frame()!);
    render({ show: false });
    render({ show: true });
    expect(frame()).toBeNull();
    expect(q("frame-quarantine")).not.toBeNull();
    expect(frame2()).not.toBeNull(); // the sibling frame is untouched
  });

  it("'Run it' remounts the frame already active — its own focus is then allowed", () => {
    focusInto(frame()!);
    act(() => q<HTMLButtonElement>("frame-quarantine-run")!.click());
    const f = frame()!;
    expect(f).not.toBeNull();
    expect(q<HTMLElement>("frame-wrap")!.dataset.active).toBe("true");
    expect(shield()).toBeNull();
    focusInto(f);
    expect(document.activeElement).toBe(f);
    expect(q("frame-quarantine")).toBeNull();
  });

  it("a re-run frame that steals again after the operator went back to the composer is quarantined again", () => {
    focusInto(frame()!);
    act(() => q<HTMLButtonElement>("frame-quarantine-run")!.click());
    const button = q<HTMLButtonElement>("page-button")!;
    act(() => button.focus()); // focus back on the page → the frame goes inactive
    expect(q<HTMLElement>("frame-wrap")!.dataset.active).toBeUndefined();
    focusInto(frame()!);
    expect(document.activeElement).toBe(button);
    expect(q("frame-quarantine")).not.toBeNull();
  });
});

describe("useFrameFocusGuard — activation", () => {
  it("an inactive frame sits under the click-catcher; a press on it activates and focuses the frame", () => {
    expect(shield()).not.toBeNull();
    act(() => {
      shield()!.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerType: "mouse", button: 0 }));
    });
    // In a real browser frame.focus() blurs this window; model that.
    act(() => {
      window.dispatchEvent(new Event("blur"));
    });
    expect(document.activeElement).toBe(frame());
    expect(shield()).toBeNull();
    expect(q("frame-quarantine")).toBeNull();
  });

  it("the shield's mousedown is cancelled, so the press doesn't move focus to the page body", () => {
    const md = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    shield()!.dispatchEvent(md);
    expect(md.defaultPrevented).toBe(true);
  });

  it("an active frame goes inactive as soon as focus returns to a page element", () => {
    act(() => {
      shield()!.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerType: "mouse", button: 0 }));
    });
    act(() => composer().focus());
    expect(shield()).not.toBeNull();
    focusInto(frame()!); // now it's a theft again
    expect(q("frame-quarantine")).not.toBeNull();
  });

  it("Tab into the frame activates it", () => {
    pressTab();
    focusInto(frame()!);
    expect(document.activeElement).toBe(frame());
    expect(q<HTMLElement>("frame-wrap")!.dataset.active).toBe("true");
  });

  it("a late out-of-process handoff inside the Tab window still counts (headed Chrome)", () => {
    pressTab();
    act(() => {
      vi.advanceTimersByTime(TAB_INTENT_MS - 50);
    });
    focusInto(frame()!);
    expect(document.activeElement).toBe(frame());
  });

  it("a self-focus after the Tab window has passed is a theft", () => {
    pressTab();
    act(() => {
      vi.advanceTimersByTime(TAB_INTENT_MS + 1);
    });
    focusInto(frame()!);
    expect(document.activeElement).toBe(composer());
    expect(q("frame-quarantine")).not.toBeNull();
  });

  it("a Tab the composer consumed (slash autocomplete) never licenses a self-focus", () => {
    pressTab(composer(), true);
    focusInto(frame()!);
    expect(document.activeElement).toBe(composer());
    expect(q("frame-quarantine")).not.toBeNull();
  });

  it("a Tab that landed on a page element is spent", () => {
    pressTab();
    const button = q<HTMLButtonElement>("page-button")!;
    act(() => button.focus());
    focusInto(frame()!);
    expect(document.activeElement).toBe(button);
    expect(q("frame-quarantine")).not.toBeNull();
  });

  it("one Tab admits one frame: another frame's self-focus after it is a theft", () => {
    pressTab();
    focusInto(frame()!);
    expect(document.activeElement).toBe(frame());
    act(() => composer().focus()); // back on the page: the Tab is spent, the frame inactive
    focusInto(frame2());
    expect(document.activeElement).toBe(composer());
    expect(frame2()).toBeNull();
    expect(frame()).not.toBeNull();
  });
});
