import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import { ApiError } from "./api";
import { themeQueryRetry, useActiveTheme } from "./useActiveTheme";

// #1916 — the selected agent theme must render on FIRST load, without an agent switch. The
// original bug: the theme query ran with `retry: false`, so the one fetch that fires while the
// focused agent is still cold (activateSlugAgent() resuming it → 409/502 from the hub proxy;
// desktop sidecar boot → fetch throws) failed permanently, and nothing ever re-applied the
// theme until a full-page agent switch re-ran the query against a warm member.

// Partial mock: only api.getTheme is swapped (per test); everything else — ApiError, is401,
// isColdStart, currentSlug (used by agentTheme) — stays the real module.
const { getTheme } = vi.hoisted(() => ({ getTheme: vi.fn<() => Promise<{ theme: unknown | null }>>() }));
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, api: { ...actual.api, getTheme: () => getTheme() } };
});

describe("themeQueryRetry — ride out cold start, give up on everything else (#1916)", () => {
  it("retries through the fleet proxy's cold-start codes (409 spawning / 502 booting)", () => {
    expect(themeQueryRetry(0, new ApiError(409, "agent not running"))).toBe(true);
    expect(themeQueryRetry(0, new ApiError(502, "member not bound"))).toBe(true);
    expect(themeQueryRetry(24, new ApiError(502, "member not bound"))).toBe(true);
    expect(themeQueryRetry(25, new ApiError(502, "member not bound"))).toBe(false); // bounded
  });

  it("retries a fetch that threw before any response (desktop sidecar boot window)", () => {
    expect(themeQueryRetry(0, new TypeError("Load failed"))).toBe(true);
  });

  it("gives up immediately on a backend without /api/theme (404 → DS defaults, no retry noise)", () => {
    expect(themeQueryRetry(0, new ApiError(404, "not found"))).toBe(false);
  });

  it("gives up immediately on 401 — the AuthGate owns recovery (#873)", () => {
    expect(themeQueryRetry(0, new ApiError(401, "unauthorized"))).toBe(false);
  });
});

describe("useActiveTheme — first load applies the theme once the fetch lands (#1916)", () => {
  let container: HTMLDivElement;
  let reactRoot: Root | null = null;

  beforeEach(() => {
    (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;
    localStorage.clear();
    getTheme.mockReset();
    document.documentElement.removeAttribute("data-theme");
    container = document.createElement("div");
    document.body.appendChild(container);
  });

  afterEach(async () => {
    if (reactRoot) {
      await act(async () => reactRoot!.unmount());
      reactRoot = null;
    }
    container.remove();
  });

  function Probe() {
    useActiveTheme();
    return null;
  }

  async function mount() {
    // retryDelay:1 stands in for the app QueryClient's backoff so the cold-start retry
    // loop resolves inside the test; the retry POLICY under test is the hook's own.
    const client = new QueryClient({ defaultOptions: { queries: { retryDelay: 1 } } });
    reactRoot = createRoot(container);
    await act(async () => {
      reactRoot!.render(createElement(QueryClientProvider, { client }, createElement(Probe)));
    });
  }

  async function until(cond: () => boolean, ms = 2000) {
    const deadline = Date.now() + ms;
    while (!cond() && Date.now() < deadline) {
      await act(async () => new Promise((r) => setTimeout(r, 10)));
    }
    expect(cond()).toBe(true);
  }

  it("applies the theme on a clean first load — no agent switch needed", async () => {
    getTheme.mockResolvedValue({ theme: { mode: "dark", overrides: { "--pl-color-accent": "#ff2266" } } });
    await mount();
    await until(() => document.documentElement.getAttribute("data-theme") === "dark");
    expect(document.documentElement.style.getPropertyValue("--pl-color-accent")).toBe("#ff2266");
  });

  it("rides out a cold-start failure and still applies the theme (the #1916 repro)", async () => {
    // First fetch races activateSlugAgent(): the member is still spawning → 502 from the
    // hub proxy. Pre-fix, retry:false made this permanent (unthemed until a switch).
    getTheme
      .mockRejectedValueOnce(new ApiError(502, "member not bound yet"))
      .mockResolvedValue({ theme: { mode: "dark", overrides: { "--pl-color-accent": "#ff2266" } } });
    await mount();
    await until(() => document.documentElement.getAttribute("data-theme") === "dark");
    expect(document.documentElement.style.getPropertyValue("--pl-color-accent")).toBe("#ff2266");
  });
});
