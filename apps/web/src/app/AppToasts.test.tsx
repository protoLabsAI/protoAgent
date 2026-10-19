import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { AppToasts } from "./AppToasts";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => vi.restoreAllMocks());

it("tracks rendered rail geometry, responsive hiding, and late shell replacement", async () => {
  let resized = () => {};
  const disconnect = vi.fn();
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { resized = callback; }
    observe() {}
    unobserve() {}
    disconnect = disconnect;
  });
  vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(800);
  let visible = true;
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const left = !this.classList.contains("pl-rail--right");
    return { width: visible ? 72 : 0, height: visible ? 600 : 0, left: left ? 0 : 728, right: left ? 72 : 800 } as DOMRect;
  });
  const container = document.createElement("div");
  const root = createRoot(container);
  const render = (shell: boolean) => <AppToasts>{shell && <div className="app-shell-main">
    <nav className="pl-rail" /><nav className="pl-rail pl-rail--right" />
  </div>}</AppToasts>;
  try {
    await act(async () => root.render(render(false)));
    const host = container.firstElementChild as HTMLElement;
    expect(host.style.getPropertyValue("--app-toast-right")).toBe("0px");
    await act(async () => root.render(render(true)));
    expect(host.style.getPropertyValue("--app-toast-left")).toBe("72px");
    expect(host.style.getPropertyValue("--app-toast-right")).toBe("72px");
    visible = false;
    act(() => resized());
    expect(host.style.getPropertyValue("--app-toast-right")).toBe("0px");
    visible = true;
    act(() => resized());
    await act(async () => root.render(render(false)));
    expect(host.style.getPropertyValue("--app-toast-left")).toBe("0px");
  } finally {
    act(() => root.unmount());
    vi.unstubAllGlobals();
  }
  expect(disconnect).toHaveBeenCalledOnce();
});
