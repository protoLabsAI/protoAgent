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

import { InlineFrameBridgeContext } from "../artifacts/ArtifactRefChip";
import { createFrameBridge } from "../artifacts/frameBridge";
import { createFrameRegistry } from "../artifacts/inlineFrames";
import {
  ComponentChatSendContext,
  FrameComponentHost,
  type ComponentChatSend,
  type FrameComponentHostProps,
} from "./FrameComponentHost";

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

  it("renders at the testid the catalog resolution + e2e target, with no allow-same-origin", () => {
    // The host carries a stable testid so ChatComponent's frame branch and the Playwright spec
    // can find it; the sandbox stays exactly allow-scripts (bearer-free opaque origin).
    mount({ id: "tid", frameUrl: "/plugins/demo/widget", kind: "pl-demo", plugin: "demo", props: {} });
    expect(container.querySelector('[data-testid="frame-component-host"]')).not.toBeNull();
    expect(theFrame().getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("keeps the survivor's live frame when one of two hosts sharing an id unmounts", () => {
    // The props doc explicitly allows two mounts of one component to share an id; they then
    // share ONE budget slot. Unmounting either must NOT drop the slot for the one left behind
    // (the regression: a single `release` used to delete the shared slot, flipping the
    // survivor permanently into the "Paused to save resources" card with nothing to re-register it).
    const registry = createFrameRegistry();
    const twin = (key: string) =>
      h(FrameComponentHost, { key, id: "shared", frameUrl: "/plugins/demo/widget", props: {}, registry });

    act(() => root.render(h("div", null, twin("a"), twin("b"))));
    // Both mounts are live → two iframes, but a single registry slot between them.
    expect(container.querySelectorAll("iframe").length).toBe(2);
    expect(registry.size()).toBe(1);

    // Unmount the first twin (key "a"); the second (key "b") is reconciled by key and preserved.
    act(() => root.render(h("div", null, twin("b"))));
    expect(registry.isLive("shared")).toBe(true);
    // The survivor still shows its LIVE iframe, not the eviction card.
    expect(container.querySelectorAll("iframe").length).toBe(1);
    expect(container.querySelector(".frame-component-host__evicted")).toBeNull();
  });
});

// The send-to-chat / openLink bridge (ADR 0118 D4 / S12b): a frame calls
// window.protoComponent.send()/openLink(); the shim relays it UP to this host as a
// protoComponent:send|openLink message (correlation id `cid`), the host runs the frameBridge
// gates (reused from the artifact inline frames) and posts the verdict back as
// protoComponent:bridgeResult. Trust is the host's, never the plugin-authored frame — so these
// drive the gates end to end through the host.
describe("FrameComponentHost — send/openLink bridge (ADR 0118 D4 / S12b)", () => {
  function setActivation(value: { isActive: boolean } | undefined) {
    if (value === undefined) {
      delete (navigator as unknown as { userActivation?: unknown }).userActivation;
    } else {
      Object.defineProperty(navigator, "userActivation", { value, configurable: true });
    }
  }
  afterEach(() => setActivation(undefined));

  function chatStub(over: Partial<ComponentChatSend> = {}): ComponentChatSend & { send: ReturnType<typeof vi.fn> } {
    return { sessionId: "s-1", isBusy: () => false, send: vi.fn(), ...over } as ComponentChatSend & {
      send: ReturnType<typeof vi.fn>;
    };
  }

  function mountBridge(chat: ComponentChatSend | null, extra: Partial<FrameComponentHostProps> = {}) {
    // A fresh bridge per mount so one test's rate window can't rate-limit the next (busy/rate
    // gates precede the gesture gate).
    act(() =>
      root.render(
        h(
          InlineFrameBridgeContext.Provider,
          { value: createFrameBridge() },
          h(
            ComponentChatSendContext.Provider,
            { value: chat },
            h(FrameComponentHost, {
              id: "cx",
              frameUrl: "/plugins/demo/widget",
              kind: "pl-demo",
              plugin: "demo",
              props: {},
              ...extra,
            }),
          ),
        ),
      ),
    );
    const frame = theFrame();
    // Capture the host's reply rather than let jsdom dispatch into the detached contentWindow.
    const post = vi.spyOn(frame.contentWindow as Window, "postMessage").mockImplementation(() => undefined as never);
    return { frame, post };
  }

  function dispatch(frame: HTMLIFrameElement, data: Record<string, unknown>, source?: unknown) {
    act(() => {
      const ev = new MessageEvent("message", { data });
      Object.defineProperty(ev, "source", { value: source ?? frame.contentWindow, configurable: true });
      window.dispatchEvent(ev);
    });
  }

  const bridgeResult = (ok: boolean, extra: Record<string, unknown> = {}) =>
    [expect.objectContaining({ type: "protoComponent:bridgeResult", cid: 7, ok, ...extra }), "*"];

  it("a gesture-backed send posts an origin-tagged user turn and resolves the frame's promise", () => {
    setActivation({ isActive: true });
    const chat = chatStub();
    const { frame, post } = mountBridge(chat);
    frame.focus(); // the gesture landed IN this frame (#4122) — an in-frame click focuses the iframe
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "  run the report  " });
    // The NORMAL send path, tagged {via:"component", kind, plugin} (via lives in ChatSessionSlot).
    expect(chat.send).toHaveBeenCalledTimes(1);
    expect(chat.send).toHaveBeenCalledWith("run the report", { kind: "pl-demo", plugin: "demo" });
    expect(post).toHaveBeenCalledWith(...bridgeResult(true, { text: "run the report" }));
    expect(container.querySelector('[data-testid="component-send-rejected"]')).toBeNull();
  });

  it("a send with NO user activation is rejected — no turn, and no bearer leaks in any reply", () => {
    setActivation({ isActive: false });
    const chat = chatStub();
    const { frame, post } = mountBridge(chat);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "do it" });
    expect(chat.send).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("click or key press") }));
    expect(container.querySelector('[data-testid="component-send-rejected"]')?.textContent).toContain("click or key press");
    // Every message the host posts on this path is bearer-free (BEARER is planted in beforeEach).
    expect(post.mock.calls.length).toBeGreaterThan(0);
    for (const [message] of post.mock.calls) expect(JSON.stringify(message)).not.toContain(BEARER);
  });

  // The gesture must land IN this frame too (ADR 0118 S16 / #4122): navigator.userActivation is
  // live for a click ANYWHERE in the console, so the host also requires document.activeElement ===
  // its own iframe. A click on console chrome leaves focus off the frame; an in-frame click moves
  // focus to it. The gate is reused from the artifact host, so this proves it holds here too.
  it("rejects a send after a click on console chrome, accepts one with focus in the frame (#4122)", () => {
    setActivation({ isActive: true });
    const chat = chatStub();
    const { frame, post } = mountBridge(chat);
    // Active user activation, but focus is on a console-chrome element, not the frame → rejected.
    const chrome = document.createElement("button");
    document.body.appendChild(chrome);
    chrome.focus();
    expect(document.activeElement).toBe(chrome);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "chrome click" });
    expect(chat.send).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("click or key press") }));
    expect(container.querySelector('[data-testid="component-send-rejected"]')?.textContent).toContain("click or key press");
    chrome.remove();
    // Now the gesture lands IN the frame (focus moves to the iframe) → accepted.
    post.mockClear();
    frame.focus();
    expect(document.activeElement).toBe(frame);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "in-frame click" });
    expect(chat.send).toHaveBeenCalledWith("in-frame click", { kind: "pl-demo", plugin: "demo" });
    expect(post).toHaveBeenCalledWith(...bridgeResult(true, { text: "in-frame click" }));
  });

  it("a send while the agent is busy is rejected with 'the agent is busy'", () => {
    setActivation({ isActive: true });
    const chat = chatStub({ isBusy: () => true });
    const { frame, post } = mountBridge(chat);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "do it" });
    expect(chat.send).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: "the agent is busy" }));
  });

  it("with no User Activation API it asks first, then posts the turn when the operator confirms", () => {
    setActivation(undefined); // runtime without navigator.userActivation
    const chat = chatStub();
    const { frame, post } = mountBridge(chat);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "run it" });
    expect(chat.send).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="component-send-confirm"]')?.textContent).toContain('Send "run it" to chat?');
    act(() => {
      container.querySelector<HTMLButtonElement>('[data-testid="component-send-confirm-ok"]')!.click();
    });
    expect(chat.send).toHaveBeenCalledWith("run it", { kind: "pl-demo", plugin: "demo" });
    expect(post).toHaveBeenCalledWith(...bridgeResult(true, { text: "run it" }));
  });

  it("cancelling the confirm prompt sends nothing and rejects the frame's promise", () => {
    setActivation(undefined);
    const chat = chatStub();
    const { frame, post } = mountBridge(chat);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "run it" });
    act(() => {
      container.querySelector<HTMLButtonElement>('[data-testid="component-send-confirm-cancel"]')!.click();
    });
    expect(chat.send).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("cancelled") }));
  });

  it("with no chat wired a send is refused rather than left hanging", () => {
    setActivation({ isActive: true });
    const { frame, post } = mountBridge(null);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "do it" });
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("can't send to chat") }));
  });

  it("a send from SOME OTHER window is ignored (e.source is the gate)", () => {
    setActivation({ isActive: true });
    const chat = chatStub();
    const { frame } = mountBridge(chat);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "do it" }, window);
    expect(chat.send).not.toHaveBeenCalled();
  });

  it("openLink opens an https link in a new tab with noopener,noreferrer, through the host", () => {
    const chat = chatStub();
    const { frame, post } = mountBridge(chat);
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    dispatch(frame, { type: "protoComponent:openLink", cid: 7, url: "https://example.com/docs" });
    expect(open).toHaveBeenCalledWith("https://example.com/docs", "_blank", "noopener,noreferrer");
    expect(post).toHaveBeenCalledWith(...bridgeResult(true));
  });

  it("openLink refuses a non-https link and opens nothing", () => {
    const chat = chatStub();
    const { frame, post } = mountBridge(chat);
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    dispatch(frame, { type: "protoComponent:openLink", cid: 7, url: "http://example.com" });
    expect(open).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("https") }));
    expect(container.querySelector('[data-testid="component-send-rejected"]')?.textContent).toContain("https");
  });
});
