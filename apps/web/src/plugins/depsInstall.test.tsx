// The install-time "install these Python packages now?" dialog and its pure helpers.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../lib/api";
import type { PluginDepsNeeded } from "../lib/types";
import {
  DepsInstallDialog,
  depsInstallErrorOutcome,
  depsInstallOutcome,
  needsPrompt,
  optionalOnlyNames,
} from "./depsInstall";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NEED: PluginDepsNeeded = {
  id: "widgets",
  name: "Widgets",
  source: "https://github.com/acme/widgets",
  target: "this server's Python environment",
  deps: [
    { name: "leftpad", spec: "leftpad>=1; sys_platform == 'darwin'", optional: false },
    { name: "fancy", spec: "fancy>=2", optional: true },
  ],
};

describe("depsInstall helpers", () => {
  it("asks only about plugins with a missing REQUIRED package", () => {
    const softOnly = { ...NEED, id: "soft", deps: [{ name: "fancy", spec: "fancy>=2", optional: true }] };
    expect(needsPrompt([NEED, softOnly]).map((n) => n.id)).toEqual(["widgets"]);
    expect(needsPrompt(undefined)).toEqual([]);
    expect(optionalOnlyNames([NEED, softOnly])).toEqual(["fancy"]);
  });

  it("maps install-deps answers to outcomes", () => {
    expect(depsInstallOutcome({ ok: true, installed: ["a"] })).toEqual({ kind: "installed", installed: ["a"] });
    expect(depsInstallOutcome({ ok: true, installed: ["a"], failed: ["b"] })).toEqual({
      kind: "partial",
      installed: ["a"],
      failed: ["b"],
    });
    expect(depsInstallOutcome({ ok: false, installed: [], failed: ["b"] }).kind).toBe("failed");
    expect(depsInstallErrorOutcome(new ApiError(409, "busy")).kind).toBe("busy");
    expect(depsInstallErrorOutcome(new ApiError(400, "pip install failed: boom"))).toEqual({
      kind: "failed",
      message: "pip install failed: boom",
    });
  });
});

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

function render(onClose = () => {}, need: PluginDepsNeeded = NEED) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => {
    root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(DepsInstallDialog, { need, onClose }))));
  });
}

const dialog = () => document.querySelector<HTMLElement>('[data-testid="plugin-deps-dialog"]');
const btn = (text: string) =>
  Array.from(document.querySelectorAll("button")).find((b) => (b.textContent || "").trim() === text);

async function click(el: HTMLElement | undefined) {
  expect(el).toBeTruthy();
  await act(async () => {
    el!.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("DepsInstallDialog", () => {
  it("lists the exact specs, the optional tier, the plugin's source and the install target", () => {
    render();
    const text = dialog()?.textContent || "";
    expect(text).toContain("leftpad>=1; sys_platform == 'darwin'");
    expect(text).toContain("fancy>=2");
    expect(text).toContain("best-effort");
    expect(text).toContain("https://github.com/acme/widgets");
    expect(text).toContain("this server's Python environment");
    expect(btn("Install packages")).toBeTruthy();
    expect(btn("Not now")).toBeTruthy();
  });

  it("on the desktop app it names the managed Python runtime as where they install", () => {
    // The frozen sidecar's install response targets the managed runtime (ADR 0094); the
    // dialog shows the server's own wording for it.
    render(() => {}, { ...NEED, target: "the desktop app's managed Python runtime" });
    expect(dialog()?.textContent || "").toContain("pip installs them into the desktop app's managed Python runtime");
  });

  it("Not now closes without installing anything", async () => {
    const call = vi.spyOn(api, "installPluginDeps");
    const onClose = vi.fn();
    render(onClose);
    await click(btn("Not now"));
    expect(onClose).toHaveBeenCalled();
    expect(call).not.toHaveBeenCalled();
  });

  it("installing → success shows the result and a Done button", async () => {
    let finish!: (v: Awaited<ReturnType<typeof api.installPluginDeps>>) => void;
    const call = vi.spyOn(api, "installPluginDeps").mockReturnValue(new Promise((r) => (finish = r)));
    render();
    await click(btn("Install packages"));
    expect(call).toHaveBeenCalledWith("widgets");
    expect(dialog()?.textContent).toContain("Installing leftpad…");
    await act(async () => {
      finish({ ok: true, installed: ["leftpad>=1", "fancy>=2"], refresh: "plugin" });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(dialog()?.textContent).toContain("Dependencies installed");
    expect(btn("Done")).toBeTruthy();
  });

  it("failure shows pip's error summary and offers Retry", async () => {
    const call = vi
      .spyOn(api, "installPluginDeps")
      .mockRejectedValueOnce(new ApiError(400, "pip install failed: No matching distribution found for leftpad>=1"))
      .mockResolvedValueOnce({ ok: true, installed: ["leftpad>=1"] });
    render();
    await click(btn("Install packages"));
    expect(dialog()?.textContent).toContain("No matching distribution found for leftpad>=1");
    await click(btn("Retry"));
    expect(call).toHaveBeenCalledTimes(2);
    expect(dialog()?.textContent).toContain("Dependencies installed");
  });
});
