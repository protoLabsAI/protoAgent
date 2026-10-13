// DownloadWatch turns the desktop shell's `download:finished` event into a toast with Open /
// Show in Finder. Mounted for real; only the Tauri globals and the toast are stubbed.

import { act, createElement as h, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Toast = { tone: string; title: string; message: unknown; duration?: number };
const mocks = vi.hoisted(() => ({ toasts: [] as Toast[] }));
vi.mock("@protolabsai/ui/overlays", () => ({
  useToast: () => (t: Toast) => {
    mocks.toasts.push(t);
    return "t1";
  },
}));

import { DownloadWatch, type DownloadFinished } from "./DownloadWatch";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLDivElement | null = null;
let emit: ((p: DownloadFinished) => void) | null = null;
const unlisten = vi.fn();
const invoke = vi.fn(async () => undefined);

function installTauri() {
  (window as unknown as { __TAURI__?: unknown }).__TAURI__ = {
    core: { invoke },
    event: {
      listen: async (name: string, fn: (e: { payload: DownloadFinished }) => void) => {
        if (name === "download:finished") emit = (p) => fn({ payload: p });
        return unlisten;
      },
    },
  };
}

async function mount() {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(h(DownloadWatch)));
}

/** Render a toast's message node so its buttons can be clicked. */
async function renderMessage(t: Toast): Promise<HTMLElement> {
  const box = document.createElement("div");
  document.body.appendChild(box);
  const r = createRoot(box);
  await act(async () => r.render(t.message as ReactElement));
  return box;
}

beforeEach(() => {
  mocks.toasts.length = 0;
  emit = null;
  unlisten.mockClear();
  invoke.mockClear();
});

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  delete (window as unknown as { __TAURI__?: unknown }).__TAURI__;
});

describe("DownloadWatch", () => {
  it("toasts a finished download, and its buttons open or reveal that exact path", async () => {
    installTauri();
    await mount();
    await act(async () => emit!({ success: true, path: "/Users/x/Downloads/chaos-helots.pdf", name: "chaos-helots.pdf" }));

    expect(mocks.toasts).toHaveLength(1);
    const t = mocks.toasts[0];
    expect(t.tone).toBe("success");
    const box = await renderMessage(t);
    expect(box.textContent).toContain("chaos-helots.pdf");

    const [open, reveal] = Array.from(box.querySelectorAll("button"));
    await act(async () => open.click());
    await act(async () => reveal.click());
    expect(invoke).toHaveBeenNthCalledWith(1, "open_download", { path: "/Users/x/Downloads/chaos-helots.pdf" });
    expect(invoke).toHaveBeenNthCalledWith(2, "reveal_download", { path: "/Users/x/Downloads/chaos-helots.pdf" });
    box.remove();
  });

  it("says a failed download failed, with no actions", async () => {
    installTauri();
    await mount();
    await act(async () => emit!({ success: false, path: null, name: "sheet.pdf" }));
    expect(mocks.toasts[0].tone).toBe("error");
    const box = await renderMessage(mocks.toasts[0]);
    expect(box.querySelectorAll("button")).toHaveLength(0);
    box.remove();
  });

  it("listens for nothing outside the desktop shell, and stops listening on unmount", async () => {
    await mount();
    expect(emit).toBeNull();
    await act(async () => root!.unmount());
    root = null;

    installTauri();
    await mount();
    await act(async () => root!.unmount());
    root = null;
    expect(unlisten).toHaveBeenCalledTimes(1);
  });
});
