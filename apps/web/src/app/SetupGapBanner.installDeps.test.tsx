// The `install_deps` setup-gap action: the loader's "can't run until its Python packages are
// installed" banner installs them right there, through the SAME POST /api/plugins/install-deps
// the Plugins row uses. createRoot/act, like SetupGapBanner.setupStep.test.tsx.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../lib/api";
import { SetupGapBanner, type SetupGap } from "./SetupGapBanner";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The gap the loader reports (graph/plugins/loader.py `_report_deps_gap`; pinned in
// tests/test_plugins.py).
function depsGap(over: Partial<SetupGap> = {}): SetupGap {
  return {
    plugin: "widgets",
    label: "Widgets",
    key: "deps-missing",
    message:
      "can't run until its Python packages are installed: leftpad. Install them from Settings ▸ Plugins or with `protoagent plugin install-deps widgets`.",
    actions: [
      { kind: "install_deps", target: "widgets", label: "Install dependencies" },
      { kind: "global_settings", target: "plugins", label: "Open Plugins" },
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
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

function render(gap: SetupGap) {
  act(() => {
    root.render(
      h(QueryClientProvider, { client }, h(ToastProvider, null, h(SetupGapBanner, { gap, onDismiss: () => {} }))),
    );
  });
}

const installButton = () => container.querySelector<HTMLButtonElement>('[data-testid="setup-gap-install-deps"]');
const bodyText = () => document.body.textContent || "";

async function click(el: HTMLElement | null) {
  expect(el).toBeTruthy();
  await act(async () => {
    el!.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("SetupGapBanner — install_deps installs the plugin's packages from the banner", () => {
  it("renders Install dependencies beside Open Plugins", () => {
    render(depsGap());
    expect(installButton()?.textContent).toContain("Install dependencies");
    expect(Array.from(container.querySelectorAll("button")).some((b) => b.textContent === "Open Plugins")).toBe(true);
  });

  it("defaults the label when the action carries none", () => {
    render(depsGap({ actions: [{ kind: "install_deps", target: "widgets" }] }));
    expect(installButton()?.textContent).toContain("Install dependencies");
  });

  it("posts THIS gap's plugin to install-deps, shows progress, then a success toast + refresh", async () => {
    let finish!: (v: Awaited<ReturnType<typeof api.installPluginDeps>>) => void;
    const call = vi.spyOn(api, "installPluginDeps").mockReturnValue(new Promise((r) => (finish = r)));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    render(depsGap());
    await click(installButton());
    expect(call).toHaveBeenCalledWith("widgets");
    // In flight: the button says so and can't be pressed twice.
    expect(installButton()?.textContent).toContain("Installing…");
    expect(installButton()?.disabled).toBe(true);
    await act(async () => {
      finish({ ok: true, installed: ["leftpad>=1"], refresh: "plugin" });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(bodyText()).toContain("Dependencies installed");
    expect(bodyText()).toContain("Widgets: leftpad>=1.");
    // Runtime status is refetched, which is what clears the (server-recomputed) banner.
    expect(invalidate).toHaveBeenCalled();
    expect(installButton()?.disabled).toBe(false);
  });

  it("a pip failure is an error toast carrying pip's summary, and the button comes back", async () => {
    vi.spyOn(api, "installPluginDeps").mockRejectedValue(
      new ApiError(400, "pip install failed: ERROR: No matching distribution found for leftpad>=1"),
    );
    render(depsGap());
    await click(installButton());
    expect(bodyText()).toContain("Dependencies didn't install");
    expect(bodyText()).toContain("No matching distribution found for leftpad>=1");
    expect(installButton()?.textContent).toContain("Install dependencies");
  });

  it("another install running (409) is an info toast, not a failure", async () => {
    vi.spyOn(api, "installPluginDeps").mockRejectedValue(new ApiError(409, "an install into this environment is running"));
    render(depsGap());
    await click(installButton());
    expect(bodyText()).toContain("A dependency install is already running");
  });

  it("an untrusted source asks for the trust ack before anything is pip'd", async () => {
    const call = vi.spyOn(api, "installPluginDeps").mockResolvedValue({ needs_ack: true, source: "github.com/rando/widgets" });
    render(depsGap());
    await click(installButton());
    expect(call).toHaveBeenCalledTimes(1);
    expect(bodyText()).toContain("github.com/rando/widgets");
    expect(bodyText()).toContain("This plugin runs code on your machine");
  });
});
