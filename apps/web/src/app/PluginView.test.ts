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

import { consoleTheme, PL_TOKEN_VARS, PluginView } from "./PluginView";
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
});
