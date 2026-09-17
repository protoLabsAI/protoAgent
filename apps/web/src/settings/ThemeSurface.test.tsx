// Settings → Theme: the host's Reset clears the persisted theme underneath the mounted DS
// ThemePanel, which reads that blob only on mount. The surface remounts the panel after a
// reset so it can't keep showing — and on the next toggle, re-save — the look just reset.
// createRoot/act + real providers, like SetupGapBanner.setupStep.test.tsx.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { themeFamilyBlob } from "@protolabsai/ui/theming";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { ThemeSurface } from "./ThemeSurface";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("style");
  document.documentElement.removeAttribute("data-theme");
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  localStorage.clear();
});

const byText = (text: string) =>
  Array.from(container.querySelectorAll("button")).find((b) => (b.textContent ?? "").trim() === text) as HTMLButtonElement;
const familyTile = (id: string) => container.querySelector(`button[data-family="${id}"]`) as HTMLButtonElement;

describe("ThemeSurface — Reset", () => {
  it("remounts the panel so a reset family is no longer shown as active", async () => {
    vi.spyOn(api, "resetTheme").mockResolvedValue({ ok: true } as never);
    localStorage.setItem("pl-theme", JSON.stringify(themeFamilyBlob("amber", "dark")));

    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    await act(async () => {
      root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(ThemeSurface))));
    });
    expect(familyTile("amber").getAttribute("aria-pressed")).toBe("true");

    await act(async () => {
      byText("Reset").click();
    });

    expect(api.resetTheme).toHaveBeenCalledOnce();
    expect(localStorage.getItem("pl-theme")).toBeNull();
    expect(familyTile("amber").getAttribute("aria-pressed")).toBe("false");
  });
});
