// The `install_node` setup-gap action: a plugin whose `requires_runtimes: {node: ">=20"}` isn't
// met gets a "needs Node.js" banner whose button provisions the managed Node runtime through
// the SAME host route the Node runtime card uses (POST /api/runtime/node/install, ADR 0085).
// createRoot/act, like SetupGapBanner.installDeps.test.tsx.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../lib/api";
import type { NodeRuntimePayload } from "../lib/types";
import { queryKeys } from "../lib/queries";
import { useUI } from "../state/uiStore";
import { SetupGapBanner, type SetupGap } from "./SetupGapBanner";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The gap the loader reports (graph/plugins/loader.py `_report_runtime_gap`; pinned in
// tests/test_node_requirement.py).
function nodeGap(over: Partial<SetupGap> = {}): SetupGap {
  return {
    plugin: "atlas",
    label: "Atlas",
    key: "runtime-missing",
    message:
      "needs Node.js >=20 (found v18.19.0). Install the managed Node runtime (v24.18.0) here, or run `protoagent runtime install-node`.",
    actions: [{ kind: "install_node", target: "atlas", label: "Install Node runtime" }],
    ...over,
  };
}

function payload(state: NodeRuntimePayload["install"]["state"], extra: Partial<NodeRuntimePayload["install"]> = {}) {
  return {
    node: {
      source: state === "done" ? "managed" : "system",
      version: state === "done" ? "v24.18.0" : "v18.19.0",
      bin_dir: "/x/bin",
      managed: state === "done",
      managed_version: state === "done" ? "v24.18.0" : null,
      system: true,
      supported: true,
      target_version: "v24.18.0",
    },
    install: { state, pct: 0, message: "", error: null, ...extra },
  } as NodeRuntimePayload;
}

let container: HTMLElement;
let root: Root;
let client: QueryClient;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  useUI.setState({ setupStepWatch: undefined });
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

const installButton = () => container.querySelector<HTMLButtonElement>('[data-testid="setup-gap-install-node"]');
const bodyText = () => document.body.textContent || "";
const flush = () => act(async () => void (await new Promise((resolve) => setTimeout(resolve, 0))));

async function click(el: HTMLElement | null) {
  expect(el).toBeTruthy();
  await act(async () => {
    el!.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("SetupGapBanner — install_node provisions the managed Node runtime from the banner", () => {
  it("renders the Install Node runtime button (and defaults the label)", () => {
    render(nodeGap());
    expect(installButton()?.textContent).toContain("Install Node runtime");
    act(() => root.unmount());
    root = createRoot(container);
    render(nodeGap({ actions: [{ kind: "install_node", target: "atlas" }] }));
    expect(installButton()?.textContent).toContain("Install Node runtime");
  });

  it("posts the host install route, arms the status watch, and toasts when the install lands", async () => {
    const post = vi.spyOn(api, "installNodeRuntime").mockResolvedValue({ ok: true, ...payload("running") });
    const status = vi
      .spyOn(api, "nodeRuntime")
      .mockResolvedValueOnce(payload("running", { pct: 40 }))
      .mockResolvedValue(payload("done", { pct: 100 }));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    render(nodeGap());
    await click(installButton());
    expect(post).toHaveBeenCalledTimes(1);
    // The runtime-status poll is armed for THIS gap, so the server's cleared gap shows up.
    expect(useUI.getState().setupStepWatch).toMatchObject({ plugin: "atlas", key: "runtime-missing" });
    // In flight: the button says so and can't be pressed twice.
    expect(installButton()?.textContent).toContain("Installing…");
    expect(installButton()?.disabled).toBe(true);
    // Drive the Node query until it reports done.
    for (let i = 0; i < 4 && !bodyText().includes("Node runtime installed"); i++) {
      await act(async () => {
        await client.refetchQueries({ queryKey: queryKeys.nodeRuntime });
      });
      await flush();
    }
    expect(status).toHaveBeenCalled();
    expect(bodyText()).toContain("Node runtime installed");
    expect(invalidate).toHaveBeenCalled();
    expect(installButton()?.disabled).toBe(false);
  });

  it("a failed install is an error toast and the button comes back", async () => {
    vi.spyOn(api, "installNodeRuntime").mockResolvedValue({ ok: true, ...payload("running") });
    vi.spyOn(api, "nodeRuntime").mockResolvedValue(payload("error", { error: "integrity check failed" }));
    render(nodeGap());
    await click(installButton());
    for (let i = 0; i < 4 && !bodyText().includes("didn't install"); i++) {
      await act(async () => {
        await client.refetchQueries({ queryKey: queryKeys.nodeRuntime });
      });
      await flush();
    }
    expect(bodyText()).toContain("Node runtime didn't install");
    expect(bodyText()).toContain("integrity check failed");
    expect(installButton()?.textContent).toContain("Install Node runtime");
  });

  it("a refused start (unsupported platform) is an error toast, not a watch", async () => {
    vi.spyOn(api, "installNodeRuntime").mockRejectedValue(
      new ApiError(400, "no managed Node build for this platform/architecture"),
    );
    render(nodeGap());
    await click(installButton());
    expect(bodyText()).toContain("Couldn't start the Node install");
    expect(useUI.getState().setupStepWatch).toBeUndefined();
    expect(installButton()?.disabled).toBe(false);
  });
});
