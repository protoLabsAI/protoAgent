import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DesignPreview } from "./DesignPreview";
import { parsePreview, previewRef } from "./preview";

const mocks = vi.hoisted(() => ({ request: vi.fn(), open: vi.fn(), post: vi.fn() }));
vi.mock("../lib/api/http", () => ({ request: mocks.request }));
vi.mock("../app/palette/nav", () => ({ openView: mocks.open }));
vi.mock("../lib/pluginViewInbox", () => ({ postToPluginView: mocks.post }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const ref = { preview_id: "a".repeat(32), title: "Design example" };
const base = { version: 1, title: "Design example", created_at: "2026-10-13T00:00:00Z" };
const palette = { ...base, kind: "palette", groups: [{ name: "Tokens", swatches: [
  { label: "--pl-color-fg", value: "#ffffff", light: "#000000", copy: "var(--pl-color-fg)" },
] }] };
const component = { ...base, kind: "component", component_title: "Primitives/Button", storybook_url: "https://stories.example.com", stories: [
  { id: "button--primary", name: "Primary", preview: "https://stories.example.com/iframe?id=button--primary", docs: "https://stories.example.com/?path=/story/button--primary" },
  { id: "button--ghost", name: "Ghost", preview: "https://stories.example.com/iframe?id=button--ghost", docs: "https://stories.example.com/?path=/story/button--ghost" },
] };
let root: Root | undefined;
let host: HTMLDivElement;
let client: QueryClient;
async function mount(props = ref) {
  host = document.createElement("div");
  document.body.append(host);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root = createRoot(host);
    root.render(createElement(QueryClientProvider, { client }, createElement(DesignPreview, { props })));
  });
  await act(async () => { await new Promise(r => setTimeout(r, 20)); });
  return host;
}
afterEach(async () => {
  await act(async () => root?.unmount());
  root = undefined;
  host?.remove();
  client?.clear();
  vi.resetAllMocks();
  vi.unstubAllGlobals();
});

describe("inline design previews", () => {
  it("renders saved swatches, switches modes, and copies the value and token", async () => {
    mocks.request.mockResolvedValue(palette);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const el = await mount();
    expect(el.textContent).toContain("Saved preview");
    expect(el.querySelector<HTMLElement>(".design-preview__color")?.style.backgroundColor).toBe("rgb(255, 255, 255)");
    await act(async () => { [...el.querySelectorAll("button")].find(b => b.textContent === "Light")!.click(); });
    expect(el.querySelector<HTMLElement>(".design-preview__color")?.style.backgroundColor).toBe("rgb(0, 0, 0)");
    await act(async () => { el.querySelector<HTMLButtonElement>('[aria-label="Copy value: #000000"]')!.click(); });
    expect(writeText).toHaveBeenCalledWith("#000000");
    await act(async () => { el.querySelector<HTMLButtonElement>('[aria-label="Copy token: var(--pl-color-fg)"]')!.click(); });
    expect(writeText).toHaveBeenCalledWith("var(--pl-color-fg)");
    expect(mocks.request).toHaveBeenCalledWith(`/api/plugins/design-system/previews/${ref.preview_id}`, expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it("renders the actual contrast pair and distinct verdicts", async () => {
    mocks.request.mockResolvedValue({ ...base, kind: "contrast", foreground: "#777777", background: "#ffffff", ratio: 4.48, aa_normal: false, aa_large: true, aaa_normal: false });
    const el = await mount();
    expect(el.textContent).toContain("AA text: Fail");
    expect(el.textContent).toContain("AA large text: Pass");
    expect(el.textContent).toContain("4.48:1");
    expect(el.querySelector<HTMLElement>(".design-preview__specimen")?.style.color).toBe("rgb(119, 119, 119)");
  });

  it("keeps large token families compact and lets the user expand them", async () => {
    mocks.request.mockResolvedValue({ ...palette, groups: [{ name: "Scale", swatches:
      Array.from({ length: 11 }, (_, i) => ({ label: String(i), value: "#663399" })) }] });
    const el = await mount();
    expect(el.querySelectorAll(".design-preview__swatch")).toHaveLength(6);
    await act(async () => { [...el.querySelectorAll("button")].find(b => b.textContent === "Show all 11 colors")!.click(); });
    expect(el.querySelectorAll(".design-preview__swatch")).toHaveLength(11);
  });

  it("uses a sandboxed live story and opens the selected variant in the playground", async () => {
    mocks.request.mockResolvedValue(component);
    const el = await mount();
    expect(el.textContent).toContain("Live Storybook preview");
    const select = el.querySelector<HTMLSelectElement>('[aria-label="Component variant"]')!;
    await act(async () => { select.value = "button--ghost"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(el.querySelector("iframe")?.src).toContain("id=button--ghost");
    expect(el.querySelector("iframe")?.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin");
    expect(mocks.open).not.toHaveBeenCalled();
    await act(async () => { [...el.querySelectorAll("button")].find(b => b.textContent === "Open playground")!.click(); });
    expect(mocks.post).toHaveBeenCalledWith("plugin:design-system:design-system", { type: "design-system:select-story", story_id: "button--ghost" });
    expect(mocks.open).toHaveBeenCalledWith("plugin:design-system:design-system");
  });

  it("keeps missing and malformed previews readable", async () => {
    mocks.request.mockRejectedValue(new Error("404"));
    expect((await mount()).textContent).toContain("Preview unavailable");
  });

  it("does not request an invalid reference", async () => {
    expect((await mount({ ...ref, preview_id: "../secrets" })).textContent).toContain("Invalid design preview");
    expect(mocks.request).not.toHaveBeenCalled();
  });
});

describe("preview data boundary", () => {
  it("rejects executable URLs, foreign story URLs, invalid colors, and oversized collections", () => {
    expect(previewRef({ ...ref, preview_id: "../x" })).toBeNull();
    expect(parsePreview({ ...component, storybook_url: "javascript:alert(1)" })).toBeNull();
    expect(parsePreview({ ...component, stories: [{ ...component.stories[0], preview: "https://other.example.com/iframe?id=button--primary" }] })).toBeNull();
    expect(parsePreview({ ...palette, groups: [{ name: "Unsafe", swatches: [{ label: "color", value: "url(https://tracker.example.com)" }] }] })).toBeNull();
    expect(parsePreview({ ...palette, groups: [{ name: "Huge", swatches: Array(513).fill(palette.groups[0].swatches[0]) }] })).toBeNull();
    expect(parsePreview({ ...palette, groups: [null] })).toBeNull();
  });
});
