import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../lib/api";
import { SetupGapBanner, type SetupGap } from "./SetupGapBanner";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function pythonGap(over: Partial<SetupGap> = {}): SetupGap {
  return {
    plugin: "widgets",
    label: "Widgets",
    key: "runtime-missing",
    message:
      "needs a Python runtime; install it to continue.",
    actions: [
      { kind: "install_python", target: "widgets", label: "Install Python runtime" },
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

const installButton = () => container.querySelector<HTMLButtonElement>('[data-testid="setup-gap-install-python"]');
const bodyText = () => document.body.textContent || "";

async function click(el: HTMLElement | null) {
  expect(el).toBeTruthy();
  await act(async () => {
    el!.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("Python runtime install action", () => {
  it("renders and posts the reporting plugin, disables while busy, and refreshes status", async () => {
    let finish!: (v: { ok: boolean }) => void;
    const call = vi.spyOn(api, "installPluginPython").mockReturnValue(new Promise((r) => (finish = r)));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    render(pythonGap({ actions: [{ kind: "install_python", target: "other" }] }));
    expect(installButton()?.textContent).toContain("Install Python runtime");
    expect(call).not.toHaveBeenCalled();
    await click(installButton());
    expect(call).toHaveBeenCalledWith("widgets");
    expect(installButton()?.disabled).toBe(true);
    await act(async () => { finish({ ok: true }); });
    expect(bodyText()).toContain("Python runtime installed");
    expect(invalidate).toHaveBeenCalled();
    expect(installButton()?.disabled).toBe(false);
  });

  it("shows errors and allows retry", async () => {
    vi.spyOn(api, "installPluginPython").mockRejectedValue(new ApiError(400, "uv missing"));
    render(pythonGap());
    await click(installButton());
    expect(bodyText()).toContain("Python runtime didn't install");
    expect(bodyText()).toContain("uv missing");
    expect(installButton()?.disabled).toBe(false);
  });

  it("asks for source consent before retrying", async () => {
    const call = vi.spyOn(api, "installPluginPython").mockResolvedValueOnce({ needs_ack: true, source: "github.com/example/widgets" }).mockResolvedValue({ ok: true });
    const ack = vi.spyOn(api, "ackPluginSource").mockResolvedValue({ ok: true, acked: "github.com/example/widgets", trust_all: false });
    render(pythonGap());
    await click(installButton());
    expect(call).toHaveBeenCalledTimes(1);
    expect(bodyText()).toContain("This plugin runs code on your machine");
    await click(Array.from(document.querySelectorAll("button")).find((b) => b.textContent === "Trust and install") ?? null);
    expect(ack).toHaveBeenCalled();
    expect(call).toHaveBeenCalledTimes(2);
  });
});
