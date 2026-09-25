// The artifact-ref chip's render states (#3617): latest, older ("v2 of 5"), deleted/evicted
// (inert), trimmed (inert), the panel off (inert), and the metadata route failing (still
// clickable). createRoot/act + the real uiStore, like the other console UI suites.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { resetPluginViewInbox, takePluginViewMessages } from "../lib/pluginViewInbox";
import { useUI } from "../state/uiStore";
import { ArtifactRefChip } from "./ArtifactRefChip";
import { ARTIFACT_VIEW_KEY } from "./artifactRef";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

async function flush() {
  for (let i = 0; i < 3; i++) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

function meta(entry: { version_count: number; oldest: number } | null) {
  return vi.spyOn(api, "artifactRefs").mockResolvedValue({
    artifacts: entry ? { "a-1": { title: "Chart", kind: "html", ...entry } } : {},
  });
}

async function mount(props: Record<string, unknown>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => root.render(h(QueryClientProvider, { client: qc }, h(ArtifactRefChip, { props }))));
  await flush();
}

const REF = { artifact_id: "a-1", version: 2, versions_total: 2, title: "Chart", kind: "html" };

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  resetPluginViewInbox();
  window.matchMedia = ((q: string) => ({ matches: false, media: q })) as never;
  useUI.setState({ railOrder: { left: ["chat"], right: [ARTIFACT_VIEW_KEY], bottom: [], hidden: [] } });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

const chip = () => container.querySelector<HTMLButtonElement>('[data-testid="artifact-ref-chip"]');

describe("ArtifactRefChip", () => {
  it("latest version: title · v2 · kind, click opens the panel on it", async () => {
    meta({ version_count: 2, oldest: 1 });
    await mount(REF);
    const btn = chip();
    expect(btn).not.toBeNull();
    expect(btn!.textContent).toContain("Chart");
    expect(btn!.textContent).toContain("v2");
    expect(btn!.textContent).not.toContain("of");
    expect(btn!.textContent).toContain("html");
    expect(btn!.dataset.older).toBeUndefined();
    act(() => btn!.click());
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([{ type: "protoArtifact:select", id: "a-1", ver: 2 }]);
    expect(useUI.getState().rightPanel).toBe(ARTIFACT_VIEW_KEY);
  });

  it("older version reads 'v2 of 5' and still opens v2", async () => {
    meta({ version_count: 5, oldest: 1 });
    await mount(REF);
    expect(chip()!.textContent).toContain("v2 of 5");
    expect(chip()!.dataset.older).toBe("true");
    act(() => chip()!.click());
    expect(takePluginViewMessages(ARTIFACT_VIEW_KEY)).toEqual([{ type: "protoArtifact:select", id: "a-1", ver: 2 }]);
  });

  it("a deleted/evicted artifact renders inert — no button, nothing opens", async () => {
    meta(null);
    await mount(REF);
    expect(chip()).toBeNull();
    const inert = container.querySelector('[data-testid="artifact-ref-gone"]');
    expect(inert?.textContent).toContain("no longer available");
  });

  it("a version trimmed at the cap renders inert", async () => {
    meta({ version_count: 60, oldest: 11 });
    await mount(REF);
    expect(chip()).toBeNull();
    expect(container.textContent).toContain("v2 of 60");
    expect(container.textContent).toContain("no longer kept");
  });

  it("with the Artifact panel off it's inert and never fetches", async () => {
    const spy = meta({ version_count: 2, oldest: 1 });
    useUI.setState({ railOrder: { left: ["chat"], right: [], bottom: [], hidden: [] } });
    await mount(REF);
    expect(chip()).toBeNull();
    expect(container.querySelector('[data-testid="artifact-ref-off"]')).not.toBeNull();
    expect(spy).not.toHaveBeenCalled();
  });

  it("a failing metadata route leaves it clickable (unknown state)", async () => {
    vi.spyOn(api, "artifactRefs").mockRejectedValue(new Error("404"));
    await mount(REF);
    expect(chip()!.textContent).toContain("v2");
  });

  it("renders a model-authored title as text, never markup", async () => {
    meta({ version_count: 2, oldest: 1 });
    await mount({ ...REF, title: '<img src=x onerror="window.__pwned=1">' });
    expect(container.querySelector("img")).toBeNull();
    expect(chip()!.textContent).toContain("<img src=x");
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("garbage props degrade to a labeled note", async () => {
    meta(null);
    await mount({ artifact_id: "", version: "x" });
    expect(container.textContent).toContain("[artifact-ref: missing artifact_id/version]");
  });
});
