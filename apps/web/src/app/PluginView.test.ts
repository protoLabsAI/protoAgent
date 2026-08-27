// The ADR 0026 theming-bridge payload (#2225): consoleTheme() must carry (1) the FULL
// computed --pl-* token map, keyed off @protolabsai/design's tokens.json, (2) the active
// light/dark `mode`, and (3) the original curated six keys — older plugin-kits bridge
// only those (their TOKEN_MAP), so they are the backward-compat contract. Also pins the
// live re-theme path: a `protoagent:theme` window event re-posts FRESH values (read at
// fire time, not captured at mount) to the mounted plugin iframe.
//
// jsdom + react-dom/client (the console has no @testing-library; the unit harness is
// `.test.ts` only, so we build elements with React.createElement rather than JSX).
// getComputedStyle is stubbed: jsdom doesn't resolve custom properties from stylesheets,
// and the stub makes every var's value deterministic — each resolves to a string derived
// from its own name (+ a `generation` counter), so asserting the value proves the exact
// var was read, and bumping the generation models an operator theme switch.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resolveMenu } from "../contextMenu/registry";
import { useContextMenuStore } from "../contextMenu/store";
import { useUI } from "../state/uiStore";
import { consoleTheme, PL_TOKEN_VARS, PluginView, pluginIdFromView, pluginMenuType } from "./PluginView";
import type { PluginView as PluginViewType } from "../lib/types";

// Tell React we're inside an act-capable environment so effect flushing is clean.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let generation = 0;
const varValue = (name: string) => `resolved(${name}:${generation})`;

beforeEach(() => {
  generation = 0;
  vi.spyOn(window, "getComputedStyle").mockImplementation(
    () => ({ getPropertyValue: varValue }) as unknown as CSSStyleDeclaration,
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  document.documentElement.removeAttribute("data-theme");
});

describe("consoleTheme() — the bridge payload (#2225)", () => {
  it("includes the full computed --pl-* map, keyed off the design package's tokens.json", () => {
    // The derived var list is the design package's real token set — spot-check
    // well-known names, including kebab-cased multi-word and nested (status) tokens.
    for (const name of [
      "--pl-color-bg", "--pl-color-bg-raised", "--pl-color-fg-muted", "--pl-color-fg-on-accent",
      "--pl-color-accent", "--pl-color-border", "--pl-color-status-error", "--pl-font-mono",
      "--pl-radius", "--pl-space-4", "--pl-motion-fast",
    ]) {
      expect(PL_TOKEN_VARS).toContain(name);
    }
    // tokens.json's top-level `light` block is mode OVERRIDES of the same names, not
    // extra tokens — it must not leak fabricated var names into the list.
    expect(PL_TOKEN_VARS.filter((n) => n.startsWith("--pl-light"))).toEqual([]);
    expect(PL_TOKEN_VARS.length).toBeGreaterThan(40); // the shipped set is ~56 — guard a broken flatten

    // Every token var lands in the snapshot with its COMPUTED value.
    const theme = consoleTheme();
    for (const name of PL_TOKEN_VARS) expect(theme[name]).toBe(varValue(name));
  });

  it("keeps the six legacy curated keys, read from the console's own vars", () => {
    const theme = consoleTheme();
    expect(theme.bg).toBe(varValue("--bg"));
    expect(theme.bgPanel).toBe(varValue("--bg-panel"));
    expect(theme.fg).toBe(varValue("--fg"));
    expect(theme.fgMuted).toBe(varValue("--fg-muted"));
    expect(theme.brand).toBe(varValue("--brand-violet-light"));
    expect(theme.border).toBe(varValue("--border"));
  });

  it("carries the active data-theme mode, falling back to the OS scheme when unforced", () => {
    // No data-theme force; jsdom's matchMedia matches no media feature → dark default.
    expect(consoleTheme().mode).toBe("dark");
    document.documentElement.setAttribute("data-theme", "light");
    expect(consoleTheme().mode).toBe("light");
    document.documentElement.setAttribute("data-theme", "dark");
    expect(consoleTheme().mode).toBe("dark");
  });
});

// The plugin id is the namespace EVERYTHING a page declares is forced into — the topics it
// publishes (ADR 0039), the chords it declares (#1457), its menu item ids (#3030) — so
// resolving it to "" doesn't degrade those features, it silently disables them.
describe("pluginIdFromView — the namespace source", () => {
  it("reads the surface key App stamps on", () => {
    expect(pluginIdFromView({ key: "plugin:boardy:board", path: "/plugins/boardy/board" })).toBe("boardy");
  });

  it("resolves a view page path — the PUBLIC namespace real plugins serve from", () => {
    // Manifests declare `path: /plugins/<id>/view` (ADR 0026); `/api/plugins/<id>/…` is the
    // plugin's gated DATA router. Matching only the /api form left every real view with no
    // namespace at all.
    expect(pluginIdFromView({ path: "/plugins/docs/view" })).toBe("docs");
    expect(pluginIdFromView({ path: "/plugins/notes/view?tab=open" })).toBe("notes");
    // …and the gated form still resolves, for a view served from the data router.
    expect(pluginIdFromView({ path: "/api/plugins/boardy/board" })).toBe("boardy");
    // A fleet-proxied slug prefix doesn't hide it either.
    expect(pluginIdFromView({ path: "/a/orbis/plugins/boardy/board" })).toBe("boardy");
  });

  it("has no namespace to offer for a path that isn't a plugin route", () => {
    expect(pluginIdFromView({ path: "/app/chat" })).toBe("");
  });
});

describe("PluginView — the protoagent:theme re-post carries updated values", () => {
  let container: HTMLElement;
  let root: Root;

  beforeEach(() => {
    // The reachability probe must succeed so the iframe mounts.
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("re-posts the fresh full payload (legacy six + --pl-* map + mode) on a theme change", async () => {
    const view: PluginViewType = {
      id: "main", label: "Test", path: "/api/plugins/testplug/main", key: "plugin:testplug:main",
    };
    await act(async () => {
      root.render(h(PluginView, { view }));
    });
    // Flush the async probe (fetch → setReachable) until the iframe is mounted.
    for (let i = 0; i < 10 && !container.querySelector("iframe"); i++) {
      await act(async () => {
        await Promise.resolve();
      });
    }
    const frame = container.querySelector("iframe");
    expect(frame).not.toBeNull();
    // Fire the iframe's load — jsdom never navigates it, but a real frame always loads
    // before the operator can touch the theme, and posts are gated on it: an unnavigated
    // frame is still about:blank on the CONSOLE's origin (`tauri://localhost` in the
    // desktop app), so a post targeted at the sidecar origin is refused by the browser.
    await act(async () => {
      frame!.dispatchEvent(new Event("load"));
    });
    const post = vi
      .spyOn(frame!.contentWindow!, "postMessage")
      .mockImplementation(() => {});

    // The operator switches theme: every var now resolves to a new value and light is
    // forced. The re-theme handler must read these at FIRE time, not mount time.
    generation = 1;
    document.documentElement.setAttribute("data-theme", "light");
    act(() => {
      window.dispatchEvent(new Event("protoagent:theme"));
    });

    const themed = post.mock.calls
      .map((c) => c[0] as { type?: string; theme?: Record<string, string> })
      .filter((m) => m?.type === "protoagent:theme");
    expect(themed.length).toBe(1);
    const theme = themed[0].theme!;
    expect(theme.mode).toBe("light");
    expect(theme.bg).toBe("resolved(--bg:1)");
    expect(theme.brand).toBe("resolved(--brand-violet-light:1)");
    for (const name of PL_TOKEN_VARS) expect(theme[name]).toBe(varValue(name));
  });

  it("posts nothing to a frame that hasn't loaded yet (about:blank inherits the console origin)", async () => {
    // Before the frame navigates it's about:blank, which INHERITS the console's origin —
    // `tauri://localhost` in the desktop app. Posting there targeted at the sidecar origin
    // is refused by the browser ("Unable to post message to http://127.0.0.1:7870.
    // Recipient has origin tauri://localhost") and nothing is listening anyway, so the
    // only effect was console noise. handleLoad posts the fresh theme once it loads.
    const view: PluginViewType = {
      id: "main", label: "Test", path: "/api/plugins/testplug/main", key: "plugin:testplug:main",
    };
    await act(async () => {
      root.render(h(PluginView, { view }));
    });
    for (let i = 0; i < 10 && !container.querySelector("iframe"); i++) {
      await act(async () => {
        await Promise.resolve();
      });
    }
    const frame = container.querySelector("iframe");
    expect(frame).not.toBeNull();
    const post = vi.spyOn(frame!.contentWindow!, "postMessage").mockImplementation(() => {});

    act(() => {
      window.dispatchEvent(new Event("protoagent:theme"));
    });
    expect(post).not.toHaveBeenCalled();

    // …and once it loads, the same event lands.
    await act(async () => {
      frame!.dispatchEvent(new Event("load"));
    });
    act(() => {
      window.dispatchEvent(new Event("protoagent:theme"));
    });
    expect(post.mock.calls.some((c) => (c[0] as { type?: string })?.type === "protoagent:theme")).toBe(true);
  });
});

// ── The context-menu bridge (#3030) ─────────────────────────────────────────────────────
// A right-click inside a plugin's iframe is invisible to the host (another document, and
// cross-origin under the desktop app), so the PAGE reports it. These pin the host half:
// what it accepts, where the menu lands, what the item does when chosen, and that nothing
// outlives the frame.
describe("PluginView — embedded Configure mode", () => {
  let container: HTMLElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("keeps the iframe bridge but ignores rail background-mount requests", async () => {
    const view: PluginViewType = {
      id: "projects",
      label: "Projects",
      path: "/plugins/boardy/config/projects",
      key: "plugin:boardy:settings:projects",
    };
    await act(async () => {
      root.render(h(PluginView, { view, embedded: true }));
    });
    for (let i = 0; i < 10 && !container.querySelector("iframe"); i++) {
      await act(async () => {
        await Promise.resolve();
      });
    }
    const frame = container.querySelector("iframe")!;
    expect(frame).toBeTruthy();
    expect(container.querySelector(".plugin-view--embedded")).toBeTruthy();
    await act(async () => {
      frame.dispatchEvent(new Event("load"));
      window.dispatchEvent(new MessageEvent("message", {
        source: frame.contentWindow,
        data: { type: "protoagent:subscribe", patterns: ["boardy.#"], background: true },
      }));
    });
    expect(useUI.getState().pluginBackground[view.key!]).toBeUndefined();
  });

  it("surfaces the owning plugin's unloaded state instead of mounting a 404 page", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 404 })));
    const view: PluginViewType = {
      id: "projects",
      label: "Projects",
      path: "/plugins/boardy/config/projects",
      key: "plugin:boardy:settings:projects",
      pluginLoaded: false,
    };
    await act(async () => {
      root.render(h(PluginView, { view, embedded: true }));
      await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });

    expect(container.querySelector("iframe")).toBeNull();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("isn’t mounted yet");
  });

  it("retries a failed page probe in place", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 404 })
      .mockResolvedValueOnce({ ok: false, status: 404 })
      .mockResolvedValueOnce({ ok: true, status: 200 });
    vi.stubGlobal("fetch", fetchMock);
    const view: PluginViewType = {
      id: "projects",
      label: "Projects",
      path: "/plugins/boardy/config/projects",
      key: "plugin:boardy:settings:projects",
    };
    await act(async () => {
      root.render(h(PluginView, { view, embedded: true }));
      await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    expect(container.querySelector('[role="alert"]')).toBeTruthy();

    const retry = [...container.querySelectorAll("button")].find((button) => button.textContent === "Retry")!;
    await act(async () => {
      retry.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.querySelector("iframe")).toBeTruthy();
  });
});

describe("PluginView — the plugin context-menu bridge (#3030)", () => {
  let container: HTMLElement;
  let root: Root;

  const view: PluginViewType = {
    id: "main", label: "Boardy", path: "/api/plugins/boardy/main", key: "plugin:boardy:main",
  };

  // Mount, flush the reachability probe, and fire the frame's load — the state every
  // bridge message arrives in.
  async function mountLoaded() {
    await act(async () => {
      root.render(h(PluginView, { view }));
    });
    for (let i = 0; i < 10 && !container.querySelector("iframe"); i++) {
      await act(async () => {
        await Promise.resolve();
      });
    }
    const frame = container.querySelector("iframe")!;
    await act(async () => {
      frame.dispatchEvent(new Event("load"));
    });
    // jsdom lays nothing out, so pin a real frame rect: the menu lands in HOST coordinates
    // (rect origin + the page's own clientX/clientY), and a 0×0 frame is treated as hidden.
    vi.spyOn(frame, "getBoundingClientRect").mockReturnValue({
      left: 300, top: 80, width: 900, height: 600,
    } as DOMRect);
    return frame;
  }

  // A message as the console sees it: from THIS frame's window (the host's trust check).
  const fromFrame = (frame: HTMLIFrameElement, data: unknown) =>
    act(() => {
      window.dispatchEvent(new MessageEvent("message", { data, source: frame.contentWindow }));
    });

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    useContextMenuStore.getState().close();
    vi.unstubAllGlobals();
  });

  it("opens the console menu for a right-click the page reported, positioned in the frame", async () => {
    const frame = await mountLoaded();

    await fromFrame(frame, {
      type: "protoagent:contextmenu:open",
      x: 120,
      y: 40,
      items: [{ id: "copy-id", label: "Copy ID", icon: "clipboard" }, { divider: true }, { id: "delete", label: "Delete", danger: true }],
    });

    const state = useContextMenuStore.getState();
    expect(state.open).toBe(true);
    expect(state.type).toBe(pluginMenuType("boardy"));
    expect([state.x, state.y]).toEqual([420, 120]);

    // What the renderer would show: the plugin's items, namespaced, plus the console's own
    // Configure… — a page that suppressed the browser menu never leaves an empty one.
    const entries = resolveMenu(state.type, state.ctx);
    expect(entries.map((e) => e.id)).toEqual([
      "plugin.boardy.copy-id",
      "plugin.boardy.delete.__divider",
      "plugin.boardy.delete",
      "plugin-view-div",
      "plugin-view-configure",
    ]);
  });

  it("clamps a page-supplied position into the frame — a plugin can't menu over the console", async () => {
    const frame = await mountLoaded();

    await fromFrame(frame, { type: "protoagent:contextmenu:open", x: 99999, y: 99999, items: [{ id: "a" }] });

    const state = useContextMenuStore.getState();
    expect([state.x, state.y]).toEqual([300 + 900, 80 + 600]);
  });

  it("posts the chosen item back with the id the PAGE knows, not the namespaced one", async () => {
    const frame = await mountLoaded();
    const post = vi.spyOn(frame.contentWindow!, "postMessage").mockImplementation(() => {});

    await fromFrame(frame, {
      type: "protoagent:contextmenu:open",
      x: 10, y: 10,
      items: [{ id: "copy-id", label: "Copy ID" }],
    });
    const state = useContextMenuStore.getState();
    const item = resolveMenu(state.type, state.ctx).find((e) => e.id === "plugin.boardy.copy-id");
    act(() => {
      (item as { run: (ctx: unknown, helpers: { close: () => void }) => void }).run(state.ctx, {
        close: () => useContextMenuStore.getState().close(),
      });
    });

    expect(post.mock.calls.map((c) => c[0])).toContainEqual({
      type: "protoagent:contextmenu:action",
      itemId: "copy-id",
    });
    expect(useContextMenuStore.getState().open).toBe(false); // choosing an item closes it
  });

  it("a bare open uses the set the page registered earlier; re-registering replaces it", async () => {
    const frame = await mountLoaded();
    await fromFrame(frame, {
      type: "protoagent:contextmenu:register",
      items: [{ id: "one", label: "One" }, { id: "two", label: "Two" }],
    });
    await fromFrame(frame, { type: "protoagent:contextmenu:open", x: 5, y: 5 });
    let state = useContextMenuStore.getState();
    expect(resolveMenu(state.type, state.ctx).map((e) => e.id)).toContain("plugin.boardy.two");

    // A view that drops an item must not leave a ghost entry firing into a page that
    // forgot about it (the keybinding bridge's rule).
    await fromFrame(frame, { type: "protoagent:contextmenu:register", items: [{ id: "one", label: "One" }] });
    await fromFrame(frame, { type: "protoagent:contextmenu:open", x: 5, y: 5 });
    state = useContextMenuStore.getState();
    const ids = resolveMenu(state.type, state.ctx).map((e) => e.id);
    expect(ids).toContain("plugin.boardy.one");
    expect(ids).not.toContain("plugin.boardy.two");
  });

  it("won't open a menu for a hidden view — a background-delivery frame measures 0×0", async () => {
    // `background: true` (#1640) keeps a view mounted while another surface is showing; App
    // display:none's it. Its right-click would otherwise land in the console's top-left.
    const frame = await mountLoaded();
    vi.spyOn(frame, "getBoundingClientRect").mockReturnValue({
      left: 0, top: 0, width: 0, height: 0,
    } as DOMRect);

    await fromFrame(frame, { type: "protoagent:contextmenu:open", x: 5, y: 5, items: [{ id: "a" }] });
    expect(useContextMenuStore.getState().open).toBe(false);
  });

  it("ignores a menu message that didn't come from this frame's window", async () => {
    const frame = await mountLoaded();
    await act(() => {
      // Another frame / an opener page trying to drive this view's menu.
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "protoagent:contextmenu:open", x: 1, y: 1, items: [{ id: "spoofed" }] },
          source: window,
        }),
      );
    });
    expect(useContextMenuStore.getState().open).toBe(false);
    void frame;
  });

  it("closes its menu and drops its registration when the view goes away", async () => {
    const frame = await mountLoaded();
    await fromFrame(frame, { type: "protoagent:contextmenu:open", x: 5, y: 5, items: [{ id: "a" }] });
    const type = useContextMenuStore.getState().type;
    expect(useContextMenuStore.getState().open).toBe(true);

    act(() => root.unmount());
    // An open menu whose items post into a dead frame must not survive it…
    expect(useContextMenuStore.getState().open).toBe(false);
    // …nor the registration that would resolve them.
    expect(resolveMenu(type, { entries: [] })).toEqual([]);

    root = createRoot(container); // afterEach unmounts again — harmless on a fresh root
  });
});
