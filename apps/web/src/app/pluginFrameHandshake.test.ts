// The bearer + theme postMessage handshake extracted from PluginView (ADR 0118 S6). These
// pin the pieces every plugin-iframe host reuses: the init post (bearer + full theme), its
// load-time re-post schedule, the live re-theme post on a `protoagent:theme` event, and the
// origin every host → frame post is targeted at (the plugin page's own origin, never "*").
//
// jsdom + react-dom/client (the console has no @testing-library; the unit harness is
// `.test.ts` only, so the theme-sync hook is driven through a tiny harness component built
// with React.createElement). getComputedStyle is stubbed so every --pl-* var resolves to a
// deterministic string derived from its own name (+ a `generation` counter): asserting the
// value proves the exact var was read, and bumping the generation models a theme switch.
import { act, createElement as h, type RefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// apiUrl is the identity here so an absolute src resolves to its own origin and a relative
// one resolves against the console origin; authToken is driven off a hoisted handle so a
// test can model "no bearer yet".
const api = vi.hoisted(() => ({ token: "bearer-abc" as string | null }));
vi.mock("../lib/api", () => ({
  apiUrl: (p: string) => p,
  authToken: () => api.token,
}));

import {
  consoleTheme,
  frameOrigin,
  INIT_REPOST_DELAYS,
  PL_TOKEN_VARS,
  postInit,
  postTheme,
  scheduleInitReposts,
  usePluginFrameThemeSync,
} from "./pluginFrameHandshake";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let generation = 0;
const varValue = (name: string) => `resolved(${name}:${generation})`;

const PLUGIN_SRC = "http://127.0.0.1:7870/api/plugins/x/main";
const PLUGIN_ORIGIN = "http://127.0.0.1:7870";

// A frame stand-in: the hook/posters only ever read `.contentWindow.postMessage`.
const fakeFrame = (post: (data: unknown, origin: string) => void) =>
  ({ contentWindow: { postMessage: post } }) as unknown as HTMLIFrameElement;

beforeEach(() => {
  generation = 0;
  api.token = "bearer-abc";
  vi.spyOn(window, "getComputedStyle").mockImplementation(
    () => ({ getPropertyValue: varValue }) as unknown as CSSStyleDeclaration,
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.documentElement.removeAttribute("data-theme");
});

describe("frameOrigin — the targeted post origin (origin checks)", () => {
  it("derives the origin from the view src, not the console origin", () => {
    expect(frameOrigin(PLUGIN_SRC)).toBe(PLUGIN_ORIGIN);
    expect(frameOrigin("https://sidecar.example:8443/plugins/x/view")).toBe("https://sidecar.example:8443");
  });

  it("resolves a relative src against the console origin", () => {
    expect(frameOrigin("/api/plugins/x/main")).toBe(window.location.origin);
  });
});

describe("postInit — the bearer + theme handshake", () => {
  it("posts protoagent:init with the bearer and full theme, targeted at the frame origin", () => {
    const post = vi.fn();
    postInit(fakeFrame(post).contentWindow!, PLUGIN_SRC);

    expect(post).toHaveBeenCalledTimes(1);
    const [payload, origin] = post.mock.calls[0] as [
      { type: string; token: string | null; theme: Record<string, string> },
      string,
    ];
    expect(payload.type).toBe("protoagent:init");
    expect(payload.token).toBe("bearer-abc");
    // The full payload rides along — the curated six, the --pl-* map, and the mode.
    expect(payload.theme.bg).toBe(varValue("--pl-color-bg"));
    expect(payload.theme.brand).toBe(varValue("--pl-color-accent"));
    expect(payload.theme.mode).toBe("dark"); // no data-theme force + no matchMedia → dark
    for (const name of PL_TOKEN_VARS) expect(payload.theme[name]).toBe(varValue(name));
    // Targeted at the plugin page's own origin — never "*".
    expect(origin).toBe(PLUGIN_ORIGIN);
  });

  it("forwards a null bearer when no token is set (the `|| null` coercion)", () => {
    api.token = "";
    const post = vi.fn();
    postInit(fakeFrame(post).contentWindow!, PLUGIN_SRC);
    expect(post.mock.calls[0][0]).toMatchObject({ type: "protoagent:init", token: null });
  });

  it("swallows a throwing post (detached / cross-origin frame — best effort)", () => {
    const win = { postMessage: () => { throw new Error("detached"); } } as unknown as Window;
    expect(() => postInit(win, PLUGIN_SRC)).not.toThrow();
  });
});

describe("scheduleInitReposts — the load-time re-post schedule", () => {
  it("posts init once immediately, then re-posts on the schedule", () => {
    vi.useFakeTimers();
    const post = vi.fn();
    const inits = () =>
      post.mock.calls.filter((c) => (c[0] as { type?: string })?.type === "protoagent:init").length;

    const timers = scheduleInitReposts(fakeFrame(post).contentWindow!, PLUGIN_SRC);
    expect(inits()).toBe(1); // immediate
    expect(timers).toHaveLength(INIT_REPOST_DELAYS.length);

    vi.advanceTimersByTime(Math.max(...INIT_REPOST_DELAYS));
    expect(inits()).toBe(1 + INIT_REPOST_DELAYS.length); // every retry fired, idempotently
  });
});

describe("postTheme — the live re-theme post", () => {
  it("posts protoagent:theme with the fresh payload, targeted at the frame origin", () => {
    const post = vi.fn();
    postTheme(fakeFrame(post).contentWindow!, PLUGIN_SRC);
    const [payload, origin] = post.mock.calls[0] as [{ type: string; theme: Record<string, string> }, string];
    expect(payload.type).toBe("protoagent:theme");
    expect(payload.theme.bg).toBe(varValue("--pl-color-bg"));
    expect(origin).toBe(PLUGIN_ORIGIN);
  });
});

describe("usePluginFrameThemeSync — re-posting theme on a theme change", () => {
  let container: HTMLElement;
  let root: Root;

  const Harness = (props: {
    frameRef: RefObject<HTMLIFrameElement | null>;
    navigatedRef: RefObject<boolean>;
    src: string;
  }) => {
    usePluginFrameThemeSync(props.frameRef, props.src, props.navigatedRef);
    return null;
  };

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  const themePosts = (post: ReturnType<typeof vi.fn>) =>
    post.mock.calls.filter((c) => (c[0] as { type?: string })?.type === "protoagent:theme");

  it("re-posts the FRESH full payload (read at fire time) on a protoagent:theme event", async () => {
    const post = vi.fn();
    const frameRef: RefObject<HTMLIFrameElement | null> = { current: fakeFrame(post) };
    const navigatedRef: RefObject<boolean> = { current: true };
    await act(async () => {
      root.render(h(Harness, { frameRef, navigatedRef, src: PLUGIN_SRC }));
    });

    // The operator switches theme: every var resolves to a new value and light is forced.
    // The handler must read these at FIRE time, not at mount time.
    generation = 1;
    document.documentElement.setAttribute("data-theme", "light");
    act(() => {
      window.dispatchEvent(new Event("protoagent:theme"));
    });

    const themed = themePosts(post);
    expect(themed).toHaveLength(1);
    expect(themed[0][0].theme.mode).toBe("light");
    expect(themed[0][0].theme.bg).toBe("resolved(--pl-color-bg:1)");
    expect(themed[0][0].theme.brand).toBe("resolved(--pl-color-accent:1)");
    for (const name of PL_TOKEN_VARS) expect(themed[0][0].theme[name]).toBe(varValue(name));
    expect(themed[0][1]).toBe(PLUGIN_ORIGIN); // still targeted, not "*"
  });

  it("skips the re-post while the frame hasn't navigated yet (about:blank, wrong origin)", async () => {
    const post = vi.fn();
    const frameRef: RefObject<HTMLIFrameElement | null> = { current: fakeFrame(post) };
    const navigatedRef: RefObject<boolean> = { current: false };
    await act(async () => {
      root.render(h(Harness, { frameRef, navigatedRef, src: PLUGIN_SRC }));
    });
    act(() => {
      window.dispatchEvent(new Event("protoagent:theme"));
    });
    expect(themePosts(post)).toHaveLength(0);
  });

  it("stops re-posting once unmounted (the listener is removed)", async () => {
    const post = vi.fn();
    const frameRef: RefObject<HTMLIFrameElement | null> = { current: fakeFrame(post) };
    const navigatedRef: RefObject<boolean> = { current: true };
    await act(async () => {
      root.render(h(Harness, { frameRef, navigatedRef, src: PLUGIN_SRC }));
    });
    act(() => root.unmount());
    act(() => {
      window.dispatchEvent(new Event("protoagent:theme"));
    });
    expect(themePosts(post)).toHaveLength(0);
  });
});

// consoleTheme is re-covered end-to-end by PluginView.test.ts (#2225); here only its
// presence as the payload the handshake carries is pinned, so a broken flatten shows up
// against the module directly.
describe("consoleTheme — the payload the handshake carries", () => {
  it("includes the curated six, the full --pl-* map, and the mode", () => {
    const theme = consoleTheme();
    expect(theme.bg).toBe(varValue("--pl-color-bg"));
    expect(theme.border).toBe(varValue("--pl-color-border"));
    expect(theme.mode).toBe("dark");
    expect(PL_TOKEN_VARS.length).toBeGreaterThan(40);
    for (const name of PL_TOKEN_VARS) expect(theme[name]).toBe(varValue(name));
  });
});
