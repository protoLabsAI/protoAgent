// The TRIPWIRE for live knowledge search in ⌘K.
//
// `knowledgeSearch.test.ts` proves the provider is correct in isolation. That is not enough,
// because a `CommandProvider` is only ever as real as the thing that CALLS it, and exactly
// one place does: `commandsView` in @protolabsai/ui is the only code in the DS that invokes
// `CommandProvider.getCommands`. A host that reimplements the palette root — which is in
// flight, since the DS root view has no ranking seam — and forgets to carry the provider
// loop across turns this whole feature into dead code that every isolated unit test still
// reports green.
//
// So these mount the REAL adapter hook, hand the registry it builds to the REAL commands
// view, type into the REAL search input, and assert a knowledge row is on screen. Anything
// that drops the provider loop reds this file, whichever module the root view lives in.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { commandsView } from "@protolabsai/ui/command-palette";
import type { PaletteContext, PaletteRegistry } from "@protolabsai/ui/command-palette";
import { act, createElement as h, useMemo } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../lib/api";
import { registerPaletteSource } from "../../ext/paletteRegistry";
import type { KnowledgeChunk } from "../../lib/types";
import type { View } from "../../lib/viewRegistry";
import { KNOWLEDGE_GROUP, KNOWLEDGE_PROVIDER_ID } from "./knowledgeSearch";
import { usePaletteRegistry } from "../usePaletteRegistry";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const chunk = (over: Partial<KnowledgeChunk> = {}): KnowledgeChunk => ({
  id: 42,
  heading: "Postgres tuning",
  content: "shared_buffers should be a quarter of RAM",
  preview: "shared_buffers should be a quarter of RAM",
  domain: "infra",
  source: "runbook.md",
  source_type: "document",
  finding_type: null,
  created_at: "2026-08-28T10:00:00+00:00",
  ...over,
});

let container: HTMLElement;
let root: Root;
let client: QueryClient;
let registry: PaletteRegistry | null = null;

const ctx: PaletteContext = {
  enter: () => {},
  back: () => {},
  close: () => {},
  props: undefined,
};

/** The console's adapter feeding the DS's own commands view — the exact pair that ships.
 *  `views` is a prop so a case can change the adapter's `navSig` and make its registration
 *  effect re-run, the way enabling a plugin does in the console. */
function Palette({ views = [] }: { views?: View[] }) {
  const built = usePaletteRegistry(views, []);
  registry = built;
  const view = useMemo(() => commandsView({ registry: built }), [built]);
  return view.render(ctx);
}

/** Type into the palette's search box the way a browser does: React listens for the native
 *  `input` event, and a controlled input only sees a value written through the prototype's
 *  setter (assigning `.value` is swallowed by React's own value tracker). */
async function type(text: string) {
  const input = container.querySelector("input")!;
  const setValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setValue.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

const rowLabels = () =>
  [...container.querySelectorAll('[role="option"]')].map((el) => el.textContent ?? "");

beforeEach(() => {
  // The fleet roster poll would otherwise hit the network on every mount; hanging it leaves
  // the hook in a state it already handles (`fleet?.agents ?? []`).
  vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>(() => {}));
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => root.render(h(QueryClientProvider, { client }, h(Palette))));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  registry = null;
  vi.restoreAllMocks();
});

describe("live knowledge search in ⌘K", () => {
  it("registers the knowledge provider on the registry the console actually builds", () => {
    expect(registry!.getProviders().map((p) => p.id)).toContain(KNOWLEDGE_PROVIDER_ID);
  });

  it("invokes the provider on a typed query and renders its rows in the palette", async () => {
    const search = vi.spyOn(api, "knowledgeSearch").mockResolvedValue({
      enabled: true,
      query: "postgres",
      results: [chunk(), chunk({ id: 43, heading: "Postgres backups", domain: "ops" })],
      stats: {},
    });

    await type("postgres");
    // The palette debounces 120ms before it calls a provider — the DS's own figure, and the
    // reason this module adds no debounce of its own.
    await vi.waitFor(() => expect(search).toHaveBeenCalled());
    expect(search.mock.calls[0][0]).toBe("postgres");

    await vi.waitFor(() => {
      const labels = rowLabels();
      expect(labels.some((l) => l.includes("Postgres tuning"))).toBe(true);
      expect(labels.some((l) => l.includes("Postgres backups"))).toBe(true);
    });
    // The group heading is what tells the operator these came from the knowledge store
    // rather than being more console commands.
    expect(container.textContent).toContain("Knowledge");
  });

  it("does not search — or list a single chunk — merely because the palette opened", async () => {
    const search = vi.spyOn(api, "knowledgeSearch").mockResolvedValue({
      enabled: true,
      query: "",
      results: [chunk()],
      stats: {},
    });
    // The root renders the console's own commands immediately; an empty `q` on that endpoint
    // is a BROWSE default (30 most-recent chunks), so a provider without the short-query
    // guard would bury them under the store's contents on every open.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 200));
    });
    expect(search).not.toHaveBeenCalled();
    expect(rowLabels().some((l) => l.includes("Postgres tuning"))).toBe(false);
  });

  it("stays LAST in provider order, so a fork's rows can't land under its heading", async () => {
    // Provider order is render order (the DS appends each provider's rows in registration
    // order) and a group header prints only when a row's group differs from the row above.
    // So a knowledge provider registered once, in an effect of its own, ends up AHEAD of the
    // source provider the moment the adapter re-registers — pushing a fork's dynamic
    // **Commands** rows below the **Knowledge** heading and printing a second **Commands**
    // header under them. Both providers ride the same effect precisely so this holds.
    const off = registerPaletteSource(() => [
      { id: "fork:row", label: "Fork row", group: "Commands", run: () => {} },
    ]);
    try {
      const last = () => registry!.getProviders().map((p) => p.id).slice(-1)[0];
      await act(async () => {}); // let the seam-version bump re-run the registration effect
      expect(registry!.getProviders().length).toBeGreaterThan(1);
      expect(last()).toBe(KNOWLEDGE_PROVIDER_ID);
      // Re-register the way enabling a plugin does — a changed view list, not a contrived poke.
      await act(async () => {
        root.render(
          h(
            QueryClientProvider,
            { client },
            h(Palette, { views: [{ id: "notes", kind: "plugin", title: "Notes" }] }),
          ),
        );
      });
      expect(last()).toBe(KNOWLEDGE_PROVIDER_ID);
      expect(KNOWLEDGE_GROUP).not.toBe("Commands"); // the heading the ordering protects
    } finally {
      off();
    }
  });

  it("shows a named failure row rather than looking like an empty result set", async () => {
    vi.spyOn(api, "knowledgeSearch").mockRejectedValue(new Error("store offline"));
    await type("postgres");
    await vi.waitFor(() => {
      expect(container.textContent).toContain("Knowledge search unavailable");
    });
    // Listed but unrunnable, with the reason on the row — the seam's convention for a row
    // that should stay discoverable and explain itself.
    const row = [...container.querySelectorAll<HTMLButtonElement>('[role="option"]')].find((el) =>
      (el.textContent ?? "").includes("Knowledge search unavailable"),
    );
    expect(row?.disabled).toBe(true);
    expect(row?.textContent).toContain("store offline");
  });
});
