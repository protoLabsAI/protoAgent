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
import { createFrameBridge, SEND_TRUST_STORAGE_KEY, type FrameBridge } from "../artifacts/frameBridge";
import { createFrameRegistry } from "../artifacts/inlineFrames";
import { resetOpenLinkOriginsCacheForTest } from "../artifacts/openLinkOrigins";
import { api } from "../lib/api";
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

// The send-to-chat / openLink bridge (ADR 0118 D4 / S12b, amended by S19 / #4137): a frame calls
// window.protoComponent.send()/openLink(); the shim relays it UP to this host as a
// protoComponent:send|openLink message (correlation id `cid`). The host reuses the artifact
// host's gate + confirm chip (FrameProposalChip): a send is a PROPOSAL the operator confirms in
// console DOM, unless they chose "Always allow from this component" (per plugin + kind).
describe("FrameComponentHost — send/openLink bridge (ADR 0118 D4 / S12b / S19)", () => {
  // The confirm chip is the DS ConfirmChip: its controls carry DS classes, not test ids, so the
  // legacy part ids (`…-confirm-ok|dismiss|trust`, `…-rejected-dismiss`) map onto those classes
  // under the host's wrapper test id.
  const CHIP_PARTS: Record<string, string> = {
    ok: ".pl-confirm-chip__confirm",
    dismiss: ".pl-confirm-chip__dismiss",
    trust: ".pl-confirm-chip__trust input",
  };
  const sel = (id: string) => {
    const m = /^(.*-(?:confirm|rejected))-(ok|dismiss|trust)$/.exec(id);
    return m ? `[data-testid="${m[1]}"] ${CHIP_PARTS[m[2]]}` : `[data-testid="${id}"]`;
  };
  const q = <T extends Element = HTMLElement>(id: string) => container.querySelector<T>(sel(id));
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  function chatStub(over: Partial<ComponentChatSend> = {}): ComponentChatSend & { send: ReturnType<typeof vi.fn> } {
    return { sessionId: "s-1", isBusy: () => false, send: vi.fn(), ...over } as ComponentChatSend & {
      send: ReturnType<typeof vi.fn>;
    };
  }

  function mountBridge(
    chat: ComponentChatSend | null,
    extra: Partial<FrameComponentHostProps> = {},
    bridge: FrameBridge = createFrameBridge(),
  ) {
    // A fresh bridge per mount (unless shared on purpose) so rate windows can't leak across tests.
    act(() =>
      root.render(
        h(
          InlineFrameBridgeContext.Provider,
          { value: bridge },
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
  const click = (id: string) => act(() => q<HTMLElement>(id)!.click());

  const bridgeResult = (ok: boolean, extra: Record<string, unknown> = {}, cid = 7) =>
    [expect.objectContaining({ type: "protoComponent:bridgeResult", cid, ok, ...extra }), "*"];

  it("a send shows the confirm chip; Send posts the origin-tagged turn exactly once", () => {
    const chat = chatStub();
    const { frame, post } = mountBridge(chat);
    frame.focus(); // self-focus no longer means anything (#4137)
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "  run the report  " });
    expect(chat.send).not.toHaveBeenCalled();
    const chip = q("component-send-confirm")!;
    // The DS ConfirmChip inside the host's wrapper: announced, but NOT modal.
    expect(chip.querySelector(".pl-confirm-chip")?.getAttribute("role")).toBe("alertdialog");
    expect(chip.querySelector(".pl-confirm-chip")?.getAttribute("aria-modal")).toBe("false");
    expect(q("component-send-confirm-text")?.textContent).toBe("run the report");
    expect(chip.textContent).toContain("Always allow from this component");
    act(() => {
      q<HTMLButtonElement>("component-send-confirm-ok")!.click();
      q<HTMLButtonElement>("component-send-confirm-ok")?.click();
    });
    // The NORMAL send path, tagged {via:"component", kind, plugin} (via lives in ChatSessionSlot).
    expect(chat.send).toHaveBeenCalledTimes(1);
    expect(chat.send).toHaveBeenCalledWith("run the report", { kind: "pl-demo", plugin: "demo" });
    expect(post).toHaveBeenCalledWith(...bridgeResult(true, { text: "run the report" }));
    // Every message the host posts on this path is bearer-free (BEARER is planted in beforeEach).
    for (const [message] of post.mock.calls) expect(JSON.stringify(message)).not.toContain(BEARER);
  });

  it("no post without a chip click — load, console click, self-focus, polling (#4137)", () => {
    let clock = 0;
    const chat = chatStub();
    const { frame } = mountBridge(chat, {}, createFrameBridge({ now: () => clock }));
    dispatch(frame, { type: "protoComponent:send", cid: 1, text: "on load" });
    const chrome = document.createElement("button");
    document.body.appendChild(chrome);
    chrome.focus();
    clock += 5000;
    dispatch(frame, { type: "protoComponent:send", cid: 2, text: "after console click" });
    chrome.remove();
    frame.focus();
    clock += 5000;
    dispatch(frame, { type: "protoComponent:send", cid: 3, text: "self focus" });
    for (let i = 0; i < 9; i++) {
      clock += 300;
      dispatch(frame, { type: "protoComponent:send", cid: 10 + i, text: `poll ${i}` });
    }
    expect(chat.send).not.toHaveBeenCalled();
    expect(container.querySelectorAll('[data-testid="component-send-confirm"]').length).toBe(1);
  });

  it("Dismiss sends nothing and rejects the frame's promise", () => {
    const chat = chatStub();
    const { frame, post } = mountBridge(chat);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "run it" });
    click("component-send-confirm-dismiss");
    expect(chat.send).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("dismissed") }));
  });

  it("'Always allow' trusts the component kind: the next send skips the chip, gates still apply", () => {
    let clock = 0;
    let busy = false;
    const chat = chatStub({ isBusy: () => busy });
    const { frame, post } = mountBridge(chat, {}, createFrameBridge({ now: () => clock }));
    dispatch(frame, { type: "protoComponent:send", cid: 1, text: "first" });
    click("component-send-confirm-trust");
    click("component-send-confirm-ok");
    expect(chat.send).toHaveBeenCalledTimes(1);
    expect(JSON.parse(localStorage.getItem(SEND_TRUST_STORAGE_KEY)!)).toEqual(["component:demo:pl-demo"]);
    clock += 5000;
    dispatch(frame, { type: "protoComponent:send", cid: 2, text: "second" });
    expect(q("component-send-confirm")).toBeNull();
    expect(chat.send).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenCalledWith(...bridgeResult(true, { text: "second" }, 2));
    // Busy still refuses a trusted kind.
    clock += 5000;
    busy = true;
    dispatch(frame, { type: "protoComponent:send", cid: 3, text: "third" });
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: "the agent is busy" }, 3));
    expect(chat.send).toHaveBeenCalledTimes(2);
  });

  it("a different component kind still prompts", () => {
    localStorage.setItem(SEND_TRUST_STORAGE_KEY, JSON.stringify(["component:demo:pl-demo"]));
    const chat = chatStub();
    const { frame } = mountBridge(chat, { kind: "pl-other" });
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "hi" });
    expect(chat.send).not.toHaveBeenCalled();
    expect(q("component-send-confirm")).not.toBeNull();
  });

  it("a send while the agent is busy is rejected with 'the agent is busy'", () => {
    const chat = chatStub({ isBusy: () => true });
    const { frame, post } = mountBridge(chat);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "do it" });
    expect(chat.send).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: "the agent is busy" }));
  });

  it("with no chat wired a send is refused rather than left hanging", () => {
    const { frame, post } = mountBridge(null);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "do it" });
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("can't send to chat") }));
  });

  it("a send from SOME OTHER window is ignored (e.source is the gate)", () => {
    const chat = chatStub();
    const { frame } = mountBridge(chat);
    dispatch(frame, { type: "protoComponent:send", cid: 7, text: "do it" }, window);
    expect(chat.send).not.toHaveBeenCalled();
    expect(q("component-send-confirm")).toBeNull();
  });

  // #4076 F1: openLink goes through the same chip, with its own "Always allow links" trust.
  async function dispatchLink(frame: HTMLIFrameElement, url: string, cid = 7) {
    await act(async () => {
      const ev = new MessageEvent("message", { data: { type: "protoComponent:openLink", cid, url } });
      Object.defineProperty(ev, "source", { value: frame.contentWindow, configurable: true });
      window.dispatchEvent(ev);
    });
  }
  beforeEach(() => {
    resetOpenLinkOriginsCacheForTest();
    vi.spyOn(api, "config").mockResolvedValue({ config: {} as never, soul: "" });
  });

  it("openLink asks first ('Open ‹host› in a new tab?') and reports success only once the tab opened", async () => {
    // Trusting this kind's SENDS must not trust its links.
    localStorage.setItem(SEND_TRUST_STORAGE_KEY, JSON.stringify(["component:demo:pl-demo"]));
    const { frame, post } = mountBridge(chatStub());
    const doc = document.implementation.createHTMLDocument("blank");
    doc.addEventListener("click", (e) => e.preventDefault());
    const tab = { opener: {} as unknown, document: doc, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockImplementation(() => tab as unknown as Window);
    await dispatchLink(frame, "https://example.com/docs");
    expect(open).not.toHaveBeenCalled();
    expect(q("component-open-confirm")?.textContent).toContain("Open example.com in a new tab?");
    expect(q("component-open-confirm")?.textContent).toContain("Always allow links from this component");
    click("component-open-confirm-ok");
    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(tab.opener).toBeNull();
    expect(post).toHaveBeenCalledWith(...bridgeResult(true));
  });

  it("a blocked pop-up or a dismiss rejects openLink with the reason", async () => {
    let now = 0;
    const { frame, post } = mountBridge(chatStub(), {}, createFrameBridge({ now: () => now }));
    vi.spyOn(window, "open").mockImplementation(() => null);
    await dispatchLink(frame, "https://example.com/docs");
    click("component-open-confirm-ok");
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringMatching(/blocked/i) }));
    now = 5_000;
    await dispatchLink(frame, "https://example.com/docs", 8);
    click("component-open-confirm-dismiss");
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringMatching(/dismissed/i) }, 8));
  });

  it("openLink refuses a non-https link and opens nothing", async () => {
    const chat = chatStub();
    const { frame, post } = mountBridge(chat);
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    await dispatchLink(frame, "http://example.com");
    expect(open).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith(...bridgeResult(false, { error: expect.stringContaining("https") }));
    expect(container.querySelector('[data-testid="component-send-rejected"]')?.textContent).toContain("https");
  });
});
