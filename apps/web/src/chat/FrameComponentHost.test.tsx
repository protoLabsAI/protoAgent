// FrameComponentHost (ADR 0118 D5 console, S12a): the sandboxed, bearer-free host for a
// frame-rendered plugin component. createRoot/act + a hand-driven jsdom, like the other
// console UI suites (the console has no testing-library dep).
//
// The four things this slice MUST get right, one `it` each:
//   • init props + theme land on load, and the theme is re-posted on a live theme change;
//   • no message EVER carries the operator bearer, and the iframe has NO allow-same-origin;
//   • a frame reporting a height is clamped to [80, 1200] px, and a spoofed source is ignored;
//   • lazy mount (only near the viewport) and the six-live-frame cap are applied.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createFrameRegistry } from "../artifacts/inlineFrames";
import { FrameComponentHost, type FrameComponentHostProps } from "./FrameComponentHost";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

const BEARER = "SECRET-BEARER-do-not-leak";

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  // A bearer the host must NEVER read or forward — planted so the leak tests are real.
  window.localStorage.setItem("protoagent.authToken", BEARER);
  // consoleTheme() reads data-theme for its `mode`; pin it so posts are deterministic.
  document.documentElement.setAttribute("data-theme", "dark");
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.documentElement.removeAttribute("data-theme");
  window.localStorage.clear();
  vi.restoreAllMocks();
});

function mount(props: FrameComponentHostProps) {
  act(() => root.render(h(FrameComponentHost, props)));
}

function theFrame(): HTMLIFrameElement {
  const frame = container.querySelector("iframe");
  if (!frame) throw new Error("no iframe mounted");
  return frame as HTMLIFrameElement;
}

/** Spy on the mounted frame's own window.postMessage, then fire its load event (the host posts
 *  the init synchronously inside onLoad). Returns the spy. */
function loadFrameWithSpy(frame: HTMLIFrameElement) {
  const win = frame.contentWindow as Window;
  const spy = vi.spyOn(win, "postMessage");
  act(() => {
    frame.dispatchEvent(new Event("load"));
  });
  return spy;
}

/** A content-height message as it would arrive FROM the frame's own window. */
function sendHeight(frame: HTMLIFrameElement, height: number, source?: unknown) {
  act(() => {
    const ev = new MessageEvent("message", { data: { type: "protoComponent:height", height } });
    Object.defineProperty(ev, "source", { value: source ?? frame.contentWindow, configurable: true });
    window.dispatchEvent(ev);
  });
}

describe("FrameComponentHost", () => {
  it("delivers init props + theme on load and re-posts the fresh theme on a theme change", () => {
    mount({ id: "c1", frameUrl: "/plugins/demo/widget", props: { title: "Hi", n: 3 } });
    const spy = loadFrameWithSpy(theFrame());

    // Init: the component props + the console theme, targeted at the opaque origin ("*").
    const init = spy.mock.calls.find((c) => (c[0] as { type?: string })?.type === "protoComponent:init");
    expect(init).toBeTruthy();
    const [initMsg, initTarget] = init as [Record<string, unknown>, string];
    expect(initMsg.props).toEqual({ title: "Hi", n: 3 });
    expect((initMsg.theme as { mode?: string }).mode).toBe("dark");
    expect(initTarget).toBe("*");

    // A live theme switch re-posts the FRESH theme (mode flips to light), without a reload.
    spy.mockClear();
    document.documentElement.setAttribute("data-theme", "light");
    act(() => {
      window.dispatchEvent(new Event("protoagent:theme"));
    });
    const reposted = spy.mock.calls.find((c) => (c[0] as { type?: string })?.type === "protoComponent:theme");
    expect(reposted).toBeTruthy();
    expect((reposted![0] as { theme: { mode: string } }).theme.mode).toBe("light");
    expect(reposted![1]).toBe("*");
  });

  it("never carries the operator bearer in any message, and the iframe has no allow-same-origin", () => {
    mount({ id: "c2", frameUrl: "/plugins/demo/widget", props: { token: "a-PROP-not-the-bearer" } });
    const frame = theFrame();

    // The sandbox is EXACTLY allow-scripts — no allow-same-origin, so the frame runs on an
    // opaque origin with no access to the console's localStorage (where the bearer lives).
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-same-origin");

    // Exercise every outbound message (init + a theme re-post), then prove none of them leak.
    const spy = loadFrameWithSpy(frame);
    document.documentElement.setAttribute("data-theme", "light");
    act(() => {
      window.dispatchEvent(new Event("protoagent:theme"));
    });

    expect(spy.mock.calls.length).toBeGreaterThan(0);
    for (const [message] of spy.mock.calls) {
      const serialized = JSON.stringify(message);
      expect(serialized).not.toContain(BEARER); // the real bearer never crosses the boundary
      // and the host adds no token/bearer/authorization key of its own (the `props.token`
      // the caller passed is its own payload, forwarded verbatim under `props`).
      const top = Object.keys(message as Record<string, unknown>);
      expect(top).not.toContain("token");
      expect(top).not.toContain("bearer");
      expect(top).not.toContain("authorization");
    }
  });

  it("clamps the reported content height and ignores a message from any other source", () => {
    mount({ id: "c3", frameUrl: "/plugins/demo/widget", props: {} });
    const frame = theFrame();
    const host = container.querySelector(".frame-component-host") as HTMLElement;

    // Starts at the floor until the frame measures.
    expect(host.style.height).toBe("80px");

    // A mid-range height is used as-is; above the ceiling clamps to 1200.
    sendHeight(frame, 500);
    expect(host.style.height).toBe("500px");
    sendHeight(frame, 5000);
    expect(host.style.height).toBe("1200px");

    // A message from a DIFFERENT window (not this frame) is ignored — the host trusts the
    // source identity, since an opaque-origin frame's `origin` is "null".
    sendHeight(frame, 999, window);
    expect(host.style.height).toBe("1200px");
  });

  it("lazy-mounts the frame only once it nears the viewport", () => {
    // A mock IntersectionObserver so the near-viewport mount is gated (jsdom has none, which
    // otherwise makes the host mount eagerly).
    const observed: Element[] = [];
    let cb: IntersectionObserverCallback | null = null;
    class MockIO {
      constructor(c: IntersectionObserverCallback) {
        cb = c;
      }
      observe(el: Element) {
        observed.push(el);
      }
      unobserve() {}
      disconnect() {}
      takeRecords(): IntersectionObserverEntry[] {
        return [];
      }
    }
    const realIO = globalThis.IntersectionObserver;
    globalThis.IntersectionObserver = MockIO as unknown as typeof IntersectionObserver;
    try {
      mount({ id: "c4", frameUrl: "/plugins/demo/widget", props: {} });
      // Off screen → no iframe yet, but the placeholder is being observed.
      expect(container.querySelector("iframe")).toBeNull();
      expect(observed.length).toBe(1);

      // Scrolls into range → the frame mounts.
      act(() => {
        cb!([{ target: observed[0], isIntersecting: true } as IntersectionObserverEntry], null as never);
      });
      expect(container.querySelector("iframe")).not.toBeNull();
    } finally {
      globalThis.IntersectionObserver = realIO;
    }
  });

  it("caps live frames at six, evicting the least-recently-visible into a static card", () => {
    const registry = createFrameRegistry();
    const hosts = Array.from({ length: 7 }, (_, i) =>
      h(FrameComponentHost, { key: `f${i}`, id: `f${i}`, frameUrl: `/plugins/demo/w${i}`, props: {}, registry }),
    );
    act(() => root.render(h("div", null, ...hosts)));

    // Seven mounted, but at most six live frames — the seventh registration evicted the
    // least-recently-visible one, which fell back to a static card.
    expect(registry.size()).toBe(6);
    expect(container.querySelectorAll("iframe").length).toBe(6);
    expect(container.querySelectorAll(".frame-component-host__evicted").length).toBe(1);
  });
});
