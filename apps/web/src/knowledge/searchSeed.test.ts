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
  takeKnowledgeSearchSeed,
  useKnowledgeSearchSeedVersion,
} from "./searchSeed";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(() => {
  takeKnowledgeSearchSeed(); // drain anything a previous case left pending
});

afterEach(() => {
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

  it("re-renders a mounted reader, so an already-open surface adopts the term too", () => {
    // Through the exported HOOK, not the raw listener set: the subscribe/getSnapshot wiring
    // is the module's own business, and a reader that never re-rendered is the failure this
    // guards — a seed arriving at an open surface with nothing watching for it.
    const seen: number[] = [];
    const Probe = () => {
      seen.push(useKnowledgeSearchSeedVersion());
      return null;
    };
    const el = document.createElement("div");
    const r = createRoot(el);
    act(() => r.render(h(Probe)));
    const rendersBefore = seen.length;
    act(() => seedKnowledgeSearch("postgres"));
    expect(seen.length).toBeGreaterThan(rendersBefore);
    expect(seen[seen.length - 1]).toBe(knowledgeSearchSeedVersion());
    act(() => r.unmount());
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
});

describe("the Knowledge surface adopts a seeded term", () => {
  let container: HTMLElement;
  let root: Root;
  let search: ReturnType<typeof vi.spyOn>;

  /** Mount the real surface the way the palette does. */
  async function mount() {
    search = vi.spyOn(api, "knowledgeSearch").mockResolvedValue({
      enabled: true,
      query: "postgres",
      results: [],
      stats: {},
    });
    // Whatever else the surface reaches for on mount (recall settings) must not reach the
    // network; hanging it leaves those panels in a loading state these assertions ignore.
    vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>(() => {}));
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
  }

  const click = async (label: string) =>
    act(async () => {
      container.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!.click();
    });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });

  it("lands already showing the seeded search, not the recent-chunks listing", async () => {
    // Seeded BEFORE the mount — the ordering the palette actually produces, since routing to
    // the surface is what mounts it.
    seedKnowledgeSearch("postgres");
    await mount();

    const input = container.querySelector<HTMLInputElement>('input[type="search"]');
    expect(input?.value).toBe("postgres");
    // `debouncedQuery` is seeded alongside `query`, so the FIRST fetch is already the seeded
    // search — no 250ms window in which the surface shows an unrelated list.
    await vi.waitFor(() => expect(search).toHaveBeenCalledWith("postgres", { reviewState: undefined }));
  });

  it("clears the review filter, so an already-open surface can still show the picked row", async () => {
    await mount();
    await click("pending review filter");
    await vi.waitFor(() => expect(search).toHaveBeenCalledWith("", { reviewState: "pending" }));

    // The surface was already open and narrowed to the review queue. Honouring the term
    // while keeping that filter is the one way this handoff can land the operator on a list
    // that does NOT contain the row they just picked.
    await act(async () => seedKnowledgeSearch("postgres"));
    await vi.waitFor(() => expect(search).toHaveBeenCalledWith("postgres", { reviewState: undefined }));
    expect(container.querySelector<HTMLButtonElement>('[aria-label="pending review filter"]')
      ?.getAttribute("aria-pressed")).toBe("false");
  });
});
