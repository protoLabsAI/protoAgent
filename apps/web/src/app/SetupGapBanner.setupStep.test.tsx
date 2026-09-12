// The `plugin_setup` setup-gap action: a banner button that runs a setup step the REPORTING
// plugin registered (POST /api/plugin-setup/<plugin>/<step> — graph/plugins/setup_gaps.py),
// and the runtime-status watch App keeps while a step it started runs server-side.
// createRoot/act + the real uiStore, like SetupGapBanner.test.tsx — no testing-library dep.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { useUI } from "../state/uiStore";
import { SetupGapBanner, setupStepWatchActive, setupStepWatchDone, type SetupGap } from "./SetupGapBanner";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The shape the host actually stores for agent_browser's missing-CLI gap
// (tests/test_agent_browser_cli_fetch.py pins the Python side of it).
function cliGap(over: Partial<SetupGap> = {}): SetupGap {
  return {
    plugin: "agent_browser",
    label: "Agent Browser",
    key: "cli",
    message: "the 'agent-browser' CLI isn't on PATH, so the browser tools and the Browser panel can't run.",
    actions: [
      { kind: "plugin_setup", target: "agent_browser", step: "download-cli", label: "Download agent-browser" },
      { kind: "plugin_config", target: "agent_browser", label: "Set the CLI path", fields: ["binary"] },
    ],
    ...over,
  };
}

let container: HTMLElement;
let root: Root;
let client: QueryClient;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  useUI.setState({ setupStepWatch: undefined, configurePlugin: undefined });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  useUI.setState({ setupStepWatch: undefined });
});

function render(gap: SetupGap) {
  act(() => {
    root.render(
      h(QueryClientProvider, { client }, h(ToastProvider, null, h(SetupGapBanner, { gap, onDismiss: () => {} }))),
    );
  });
}

const button = (text: string) =>
  Array.from(container.querySelectorAll("button")).find((b) => (b.textContent || "").trim() === text);

async function click(el: HTMLElement | undefined) {
  expect(el).toBeTruthy();
  await act(async () => {
    el!.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("SetupGapBanner — a plugin_setup action runs the reporting plugin's step", () => {
  it("renders the step's label beside the config CTA", () => {
    render(cliGap());
    expect(button("Download agent-browser")).toBeTruthy();
    expect(button("Set the CLI path")).toBeTruthy();
  });

  it("POSTs THIS gap's plugin + step, arms the status watch on `pending`, and refreshes status", async () => {
    const run = vi
      .spyOn(api, "runPluginSetupStep")
      .mockResolvedValue({ ok: true, pending: true, message: "Downloading agent-browser v0.27.1" });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    render(cliGap());
    await click(button("Download agent-browser"));
    expect(run).toHaveBeenCalledWith("agent_browser", "download-cli");
    expect(useUI.getState().setupStepWatch).toMatchObject({ plugin: "agent_browser", key: "cli" });
    expect(setupStepWatchActive()).toBe(true);
    expect(invalidate).toHaveBeenCalled();
  });

  it("a step that finished at once, or failed, arms no watch", async () => {
    const run = vi
      .spyOn(api, "runPluginSetupStep")
      .mockResolvedValueOnce({ ok: true, message: "agent-browser is already on PATH" })
      .mockResolvedValueOnce({ ok: false, message: "no build for this platform" });
    render(cliGap());
    await click(button("Download agent-browser"));
    await click(button("Download agent-browser"));
    expect(run).toHaveBeenCalledTimes(2);
    expect(useUI.getState().setupStepWatch).toBeUndefined();
  });

  it("a rejected request (the plugin was disabled → 404) is an error toast, not a crash", async () => {
    vi.spyOn(api, "runPluginSetupStep").mockRejectedValue(new Error("plugin 'agent_browser' has no setup step"));
    render(cliGap());
    await click(button("Download agent-browser"));
    expect(button("Download agent-browser")).toBeTruthy(); // re-enabled, still offered
    expect(useUI.getState().setupStepWatch).toBeUndefined();
  });

  it("renders no button for a plugin_setup action without a step", () => {
    const run = vi.spyOn(api, "runPluginSetupStep");
    render(cliGap({ actions: [{ kind: "plugin_setup", label: "Go" }] }));
    expect(button("Go")).toBeUndefined();
    expect(run).not.toHaveBeenCalled();
  });
});

describe("setupStepWatchDone — when App stops polling for a banner-started step", () => {
  const watch = { plugin: "agent_browser", key: "cli", since: 1000, until: 61_000 };
  const inProgress = cliGap({ message: "downloading the agent-browser CLI v0.27.1", actions: undefined });

  it("keeps polling while the gap shows progress (no button)", () => {
    expect(setupStepWatchDone(watch, [inProgress], true, 2000, 2000)).toBe(false);
  });

  it("ignores a status fetched before the click — it still shows the button just pressed", () => {
    expect(setupStepWatchDone(watch, [cliGap()], true, 999, 2000)).toBe(false);
  });

  it("stops once the gap is gone (done) or offers its button again (failed → Retry)", () => {
    expect(setupStepWatchDone(watch, [], true, 2000, 2000)).toBe(true);
    expect(setupStepWatchDone(watch, [cliGap()], true, 2000, 2000)).toBe(true);
  });

  it("never decides on an unknown gap list, and always stops once expired", () => {
    expect(setupStepWatchDone(watch, [], false, 2000, 2000)).toBe(false);
    expect(setupStepWatchDone(watch, [inProgress], true, 2000, watch.until)).toBe(true);
    expect(setupStepWatchDone(undefined, [], true, 2000, 2000)).toBe(true);
  });
});
