// The one-shot search-term handoff into the Knowledge surface, and the NavIntent arm that
// drives it. Both halves matter for the ⌘K knowledge rows (ADR 0057): the intent is what
// makes the row work in the frameless desktop launcher (a serializable payload the launcher
// forwards to the real console window), and the one-shot semantics are what keep a seeded
// term from re-narrowing the surface behind the operator's back on a later visit.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { Suspense } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { applyNavIntent } from "../app/usePaletteRegistry";
import { api } from "../lib/api";
import { useUI } from "../state/uiStore";
import { KnowledgeStore } from "./KnowledgeStore";
import {
  knowledgeSearchSeedVersion,
  seedKnowledgeSearch,
  subscribeKnowledgeSearchSeed,
  takeKnowledgeSearchSeed,
} from "./searchSeed";

const offs: (() => void)[] = [];

beforeEach(() => {
  takeKnowledgeSearchSeed(); // drain anything a previous case left pending
});

afterEach(() => {
  offs.splice(0).forEach((off) => off());
  takeKnowledgeSearchSeed();
});

describe("knowledge search seed", () => {
  it("is consumed on read, so a later remount cannot replay it over live typing", () => {
    seedKnowledgeSearch("postgres");
    expect(takeKnowledgeSearchSeed()).toBe("postgres");
    expect(takeKnowledgeSearchSeed()).toBeNull();
  });

  it("bumps a version on every seed, including the same term twice", () => {
    // The surface keys its adopt-effect on the VERSION: re-picking the same ⌘K row after
    // typing something else has to re-apply, and a value-keyed effect would not fire.
    const before = knowledgeSearchSeedVersion();
    seedKnowledgeSearch("postgres");
    takeKnowledgeSearchSeed();
    seedKnowledgeSearch("postgres");
    expect(knowledgeSearchSeedVersion()).toBe(before + 2);
  });

  it("notifies subscribers so an already-open surface adopts the term too", () => {
    let seen = 0;
    offs.push(subscribeKnowledgeSearchSeed(() => (seen += 1)));
    seedKnowledgeSearch("postgres");
    expect(seen).toBe(1);
  });
});

describe("the knowledge NavIntent arm", () => {
  it("seeds the term and routes to the Knowledge surface", () => {
    applyNavIntent({ kind: "knowledge", query: "postgres" });
    // Seeded BEFORE the route, so the surface adopts it in its first effect rather than
    // painting the unrelated recent-chunks listing first.
    expect(takeKnowledgeSearchSeed()).toBe("postgres");
    expect(useUI.getState().surface).toBe("knowledge");
    // The mobile shell reads `mobileActive`, not the per-dock ids — without it every
    // programmatic navigation is a silent no-op on a phone.
    expect(useUI.getState().mobileActive).toBe("knowledge");
  });

  it("routes with an empty term when the intent carries none", () => {
    applyNavIntent({ kind: "knowledge" });
    expect(takeKnowledgeSearchSeed()).toBe("");
    expect(useUI.getState().surface).toBe("knowledge");
  });
});

describe("the Knowledge surface adopts a seeded term", () => {
  let container: HTMLElement;
  let root: Root;

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });

  it("lands already showing the seeded search, not the recent-chunks listing", async () => {
    const search = vi.spyOn(api, "knowledgeSearch").mockResolvedValue({
      enabled: true,
      query: "postgres",
      results: [],
      stats: {},
    });
    // Whatever else the surface reaches for on mount (recall settings) must not reach the
    // network; hanging it leaves those panels in a loading state this assertion ignores.
    vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>(() => {}));

    // Seeded BEFORE the mount — the ordering the palette actually produces, since routing to
    // the surface is what mounts it.
    seedKnowledgeSearch("postgres");
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    await act(async () => {
      root.render(
        h(
          QueryClientProvider,
          { client },
          h(ToastProvider, null, h(Suspense, { fallback: null }, h(KnowledgeStore))),
        ),
      );
    });

    const input = container.querySelector<HTMLInputElement>('input[type="search"]');
    expect(input?.value).toBe("postgres");
    // `debouncedQuery` is seeded alongside `query`, so the FIRST fetch is already the seeded
    // search — no 250ms window in which the surface shows an unrelated list.
    await vi.waitFor(() => expect(search).toHaveBeenCalledWith("postgres", { reviewState: undefined }));
  });
});
