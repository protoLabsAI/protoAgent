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
  vi.spyOn(api, "getTheme").mockResolvedValue({ theme: null });
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
    await act(async () => { familyTile("teal").click(); });
    expect(container.textContent).toContain("Not saved to this agent");

    await act(async () => {
      byText("Reset to default").click();
    });

    expect(api.resetTheme).toHaveBeenCalledOnce();
    expect(localStorage.getItem("pl-theme")).toBeNull();
    expect(container.textContent).not.toContain("Not saved to this agent");
    expect(familyTile("amber").getAttribute("aria-pressed")).toBe("false");
  });
});


describe("ThemeSurface — unsaved look", () => {
  it.each([false, true])("tracks edits and only clears after a successful save (failure: %s)", async (fails) => {
    const theme = themeFamilyBlob("amber", "dark");
    let savedTheme: unknown = theme;
    vi.mocked(api.getTheme).mockImplementation(async () => ({ theme: savedTheme }));
    vi.spyOn(api, "saveTheme").mockImplementation(async (next) => {
      if (fails) throw new Error("Offline");
      savedTheme = next;
      return { ok: true };
    });
    localStorage.setItem("pl-theme", JSON.stringify(theme));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    client.setQueryData(["theme"], { theme });
    await act(async () => {
      root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(ThemeSurface))));
    });
    expect(container.textContent).toContain("How this agent's console looks.");
    expect(byText("Reset to default")).toBeDefined();
    expect(container.textContent).not.toContain("Not saved to this agent");

    await act(async () => { familyTile("teal").click(); });
    expect(container.textContent).toContain("Not saved to this agent");
    await act(async () => { familyTile("amber").click(); });
    expect(container.textContent).not.toContain("Not saved to this agent");
    await act(async () => { familyTile("teal").click(); });
    await act(async () => { byText("Save to this agent").click(); });
    expect(container.textContent?.includes("Not saved to this agent")).toBe(fails);
    expect(container.textContent).toContain(fails ? "Save failed" : "Theme saved");
    expect((savedTheme as { preset: string }).preset).toBe(fails ? "amber" : "teal");
  });
});
