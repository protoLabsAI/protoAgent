// Console-level coverage for the mobile touch-tap bug on Settings dropdowns
// (protoLabsAI/protoAgent#4534). The Settings panels pick with the DS `DropdownSelect`
// (@protolabsai/ui/forms) — a Radix dropdown-menu. On touch devices the opening tap's
// trailing `click` dismissed the menu it had just opened (root cause + fix:
// protoLabsAI/protoContent#638), so one tap opened and immediately closed it.
//
// PENDING — intentionally skipped until this console depends on the `@protolabsai/ui`
// release that carries protoContent#638 (expected >= 0.68.5). The fix ships in the DS,
// not here; this suite is the console-side guard that proves every Settings dropdown
// inherited it once the dependency is bumped. Flip `describe.skip` -> `describe` in the
// same PR that bumps `@protolabsai/ui`. Until then it can't pass: the installed DS still
// opens on pointerdown.
//
// Same render pattern as ProvidersPanel.ui.test.ts: react-dom/client + act in jsdom, with
// the pointer-capture / scrollIntoView stubs jsdom lacks so a Radix menu can open at all.

import { DropdownSelect } from "@protolabsai/ui/forms";
import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// jsdom ships no layout / pointer-capture, so a Radix DropdownMenu never opens under it
// without these (see ProvidersPanel.ui.test.ts for the same gap).
Element.prototype.hasPointerCapture ??= () => false;
Element.prototype.releasePointerCapture ??= () => {};
Element.prototype.scrollIntoView ??= () => {};

// A mobile (touch) viewport: narrow width + a matchMedia that answers max-width queries true.
function mobileViewport() {
  Object.defineProperty(window, "innerWidth", { value: 390, configurable: true });
  Object.defineProperty(window, "innerHeight", { value: 844, configurable: true });
  window.matchMedia ??= ((query: string) =>
    ({
      matches: /max-width/.test(query),
      media: query,
      onchange: null,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
      dispatchEvent() {
        return false;
      },
    }) as unknown as MediaQueryList) as typeof window.matchMedia;
}

// Browser-faithful pointerdown: PointerEvent when the env has it, else a MouseEvent with
// pointerType defined on it (all Radix and the DS handler read off the native event).
async function pointerDown(el: Element, pointerType: "touch" | "mouse") {
  await act(async () => {
    const Ctor = (window as unknown as { PointerEvent?: typeof PointerEvent }).PointerEvent;
    let e: Event;
    if (Ctor) {
      e = new Ctor("pointerdown", { pointerType, button: 0, bubbles: true, cancelable: true } as PointerEventInit);
    } else {
      e = new MouseEvent("pointerdown", { button: 0, bubbles: true, cancelable: true });
    }
    if ((e as PointerEvent).pointerType !== pointerType) {
      Object.defineProperty(e, "pointerType", { value: pointerType, configurable: true });
    }
    el.dispatchEvent(e);
  });
}

async function click(el: Element) {
  await act(async () => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail: 1 }));
  });
}

const tap = async (el: Element) => {
  await pointerDown(el, "touch");
  await click(el);
};

let container: HTMLElement;
let root: Root;

// A Settings-style single-select (cf. McpPanel transport, TasksPanel priority, ProvidersPanel).
const options = [
  { value: "stdio", label: "stdio" },
  { value: "http", label: "HTTP" },
  { value: "sse", label: "SSE" },
];
const dropdownOpen = () => Boolean(document.querySelector(".pl-dropselect__menu"));

beforeEach(() => {
  mobileViewport();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.body.innerHTML = "";
});

describe.skip("Settings dropdowns on touch / mobile viewport (#4534 — unskip after @protolabsai/ui bump)", () => {
  it("one tap opens the dropdown and it stays open", async () => {
    await act(async () => root.render(h(DropdownSelect, { options, value: "stdio", onValueChange() {}, "aria-label": "Transport" })));
    const trigger = container.querySelector(".pl-dropselect")!;
    await pointerDown(trigger, "touch");
    expect(dropdownOpen()).toBe(false); // Radix's pointerdown-open is suppressed on touch
    await click(trigger);
    expect(dropdownOpen()).toBe(true); // the tap's click opens it — and it stays open
  });

  it("a second tap closes it (toggle still works)", async () => {
    await act(async () => root.render(h(DropdownSelect, { options, value: "stdio", onValueChange() {}, "aria-label": "Transport" })));
    const trigger = container.querySelector(".pl-dropselect")!;
    await tap(trigger);
    expect(dropdownOpen()).toBe(true);
    await tap(trigger);
    expect(dropdownOpen()).toBe(false);
  });

  it("desktop mouse is unchanged: pointerdown opens and the trailing click does not close it", async () => {
    await act(async () => root.render(h(DropdownSelect, { options, value: "stdio", onValueChange() {}, "aria-label": "Transport" })));
    const trigger = container.querySelector(".pl-dropselect")!;
    await pointerDown(trigger, "mouse");
    expect(dropdownOpen()).toBe(true);
    await click(trigger);
    expect(dropdownOpen()).toBe(true);
  });
});
