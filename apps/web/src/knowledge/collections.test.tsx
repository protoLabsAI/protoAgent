import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import type { KnowledgeChunk } from "../lib/types";
import { CollectionsBrowser, highlight, sortedCollections } from "./collections";
import { useCollections, type Collection } from "./useCollections";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const rows: Collection[] = [
  { collection: "alpha", chunks: 51, sources: 1, last_ingested_at: "2026-10-01" },
  { collection: "beta", chunks: 2, sources: 1, last_ingested_at: "2026-11-01" },
];
const chunk = (id: number): KnowledgeChunk => ({ id, heading: `Chunk ${id}`, content: `Safety note ${id}`, preview: "Safety", domain: "legacy", source: "Manual", source_type: "file", finding_type: null, created_at: null });
let host: HTMLDivElement, root: Root, qc: QueryClient, model: ReturnType<typeof useCollections>;
function Harness() {
  model = useCollections();
  return <CollectionsBrowser model={model} renderCard={c => <li key={c.id}>{highlight(c.content, model.term)}</li>} onAdd={() => {}} onSettings={() => {}} />;
}
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); }); }
async function render() {
  await act(async () => root.render(<QueryClientProvider client={qc}><ToastProvider><Harness /></ToastProvider></QueryClientProvider>));
  await settle();
}
function button(text: string) {
  const result = Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find(el => el.textContent === text);
  if (!result) throw new Error(`Button missing: ${text}`);
  return result;
}
async function click(text: string) { await act(async () => button(text).click()); await settle(); }
beforeEach(() => {
  window.history.replaceState({}, "", "/app/");
  host = document.createElement("div"); document.body.appendChild(host); root = createRoot(host);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  vi.spyOn(api, "listCollections").mockResolvedValue({ enabled: true, collections: rows });
  vi.spyOn(api, "knowledgeSearch").mockResolvedValue({ enabled: true, query: "", results: Array.from({ length: 51 }, (_, i) => chunk(i + 1)), stats: {} });
});
afterEach(() => { act(() => root.unmount()); host.remove(); qc.clear(); vi.restoreAllMocks(); });

describe("collection browser", () => {
  it("renders the collections table with a caption, default size order, and no invented retrieval figures", async () => {
    await render();
    expect(host.querySelector("caption")?.textContent).toContain("Knowledge collections");
    expect(host.textContent).toContain("2 collections · 53 chunks");
    expect(host.textContent).toContain("Not tracked yet");
    expect(host.querySelector('[role="progressbar"]')).toBeNull();
    expect(host.textContent).not.toContain("supplies");
    expect(host.querySelector('th[aria-sort="descending"]')?.textContent).toBe("Size");
    await click("Size");
    expect(host.querySelector("tbody tr a")?.textContent).toBe("beta");
    await click("Collection");
    expect(host.querySelector('th[aria-sort="ascending"]')?.textContent).toBe("Collection");
    expect(sortedCollections(rows, "updated", false)[0].collection).toBe("beta");
  });
  it("renders loading without an empty-state claim", async () => {
    vi.mocked(api.listCollections).mockReturnValue(new Promise(() => {}));
    await render(); expect(host.querySelector('[aria-label="Loading collections"]')).not.toBeNull();
    expect(host.textContent).not.toContain("No knowledge yet");
  });
  it.each([[false, "Knowledge is off for this agent"], [true, "No knowledge yet"]])("renders enabled=%s with no collections", async (enabled, title) => {
    vi.mocked(api.listCollections).mockResolvedValue({ enabled, collections: [] });
    await render(); expect(host.textContent).toContain(title);
  });
  it("renders one collection without the dominant-share callout", async () => {
    vi.mocked(api.listCollections).mockResolvedValue({ enabled: true, collections: [{ ...rows[0], retrieved_share_7d: 1 } as Collection] });
    await render(); expect(host.querySelectorAll("tbody tr")).toHaveLength(1); expect(host.textContent).not.toContain("supplies");
  });
  it("keeps a failed load distinct from empty and supports retry", async () => {
    vi.mocked(api.listCollections).mockRejectedValueOnce(new Error("Offline"));
    await render(); expect(host.textContent).toContain("Couldn't load collections"); expect(host.textContent).not.toContain("No knowledge yet");
    await click("Retry"); expect(host.querySelectorAll("tbody tr")).toHaveLength(2);
  });
  it("opens a URL-backed detail and pages a document in groups of 50", async () => {
    await render(); await act(async () => model.open("alpha")); await settle();
    expect(window.location.search).toBe("?collection=alpha");
    expect(host.querySelectorAll(".playbook-list li")).toHaveLength(50);
    await click("Show the next 50"); expect(host.querySelectorAll(".playbook-list li")).toHaveLength(51);
    expect(api.knowledgeSearch).toHaveBeenCalledWith("", expect.objectContaining({ collection: "alpha", k: 51 }));
    window.history.replaceState({}, "", "/app/"); await act(async () => window.dispatchEvent(new PopStateEvent("popstate"))); await settle();
    expect(host.querySelector("table")).not.toBeNull();
  });
  it("restores a deep link and renders empty, gone, and failed details", async () => {
    window.history.replaceState({}, "", "/app/?collection=alpha");
    vi.mocked(api.knowledgeSearch).mockResolvedValue({ enabled: true, query: "", results: [], stats: {} });
    await render(); expect(host.textContent).toContain("This collection is empty.");
    await act(async () => model.open("missing")); await settle(); expect(host.textContent).toContain("isn't here any more");
    vi.mocked(api.knowledgeSearch).mockRejectedValue(new Error("Offline"));
    await act(async () => model.open("beta")); await settle(); expect(host.textContent).toContain("Couldn't load beta");
  });
  it("debounces scoped search and can broaden an empty search across collections", async () => {
    vi.mocked(api.knowledgeSearch).mockResolvedValue({ enabled: true, query: "", results: [], stats: {} });
    await render(); await act(async () => { model.open("alpha"); model.setQuery("needle"); });
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 300)); }); await settle();
    expect(api.knowledgeSearch).toHaveBeenCalledWith("needle", expect.objectContaining({ collection: "alpha" }));
    expect(host.textContent).toContain('Nothing in alpha matches "needle"');
    await click("Search all collections");
    expect(api.knowledgeSearch).toHaveBeenCalledWith("needle", expect.objectContaining({ collection: "beta" }));
    expect(host.textContent).toContain("Nothing in this agent's knowledge");
    await act(async () => model.setQuery("")); await act(async () => { await new Promise(resolve => setTimeout(resolve, 300)); });
    expect(host.textContent).toContain("This collection is empty.");
  });
  it("deletes, offers a 10-second undo, and restores through the collection routes", async () => {
    const timers = vi.spyOn(globalThis, "setTimeout");
    const remove = vi.spyOn(api, "deleteKnowledgeByCollection").mockImplementation(async () => {
      vi.mocked(api.listCollections).mockResolvedValue({ enabled: true, collections: [rows[1]] });
      return { enabled: true, deleted: 51 };
    });
    const restore = vi.spyOn(api, "restoreKnowledgeByCollection").mockImplementation(async () => {
      vi.mocked(api.listCollections).mockResolvedValue({ enabled: true, collections: rows });
      return { enabled: true, restored: 51 };
    });
    await render(); await act(async () => model.open("alpha")); await settle(); await click("Delete collection…");
    expect(document.body.textContent).toContain("This removes all 51 chunks in 1 documents");
    await click("Delete collection"); expect(remove).toHaveBeenCalledWith("alpha");
    expect(document.body.textContent).toContain("Deleted alpha (51 chunks).");
    expect(timers).toHaveBeenCalledWith(expect.any(Function), 10000);
    expect(host.querySelectorAll("tbody tr")).toHaveLength(1);
    await click("Undo"); expect(restore).toHaveBeenCalledWith("alpha");
    expect(host.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(document.body.textContent).toContain("Restored alpha.");
  });
  it("keeps the destructive dialog open with a warning and Retry on an API-level error", async () => {
    vi.spyOn(api, "deleteKnowledgeByCollection").mockResolvedValue({ enabled: true, deleted: 0, error: "Busy" });
    await render(); await act(async () => model.open("alpha")); await settle(); await click("Delete collection…"); await click("Delete collection");
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain("Couldn't delete alpha. Nothing was removed.");
    expect(button("Try again")).toBeTruthy();
    expect(model.collections).toHaveLength(2);
  });
  it("does not announce deletion when a nonempty collection returns zero removed", async () => {
    vi.spyOn(api, "deleteKnowledgeByCollection").mockResolvedValue({ enabled: true, deleted: 0 });
    await render(); await act(async () => model.open("alpha")); await settle();
    await click("Delete collection…"); await click("Delete collection");
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain("Nothing was removed.");
    expect(document.body.textContent).not.toContain("Deleted alpha");
  });
  it("refuses to delete when the chunk read can't be confirmed (store error returns an empty list)", async () => {
    const remove = vi.spyOn(api, "deleteKnowledgeByCollection");
    // The /search route never 500s: a server-side store error comes back as a 200 with an
    // empty result list, indistinguishable from a genuinely empty bucket. alpha claims 51
    // chunks, so an empty read is unverifiable — the guard must refuse, not fail open.
    vi.mocked(api.knowledgeSearch).mockResolvedValue({ enabled: true, query: "", results: [], stats: {} });
    await render();
    await act(async () => { await model.remove.mutateAsync("alpha").catch(() => {}); });
    await settle();
    expect(remove).not.toHaveBeenCalled();
    expect(model.remove.error?.message).toContain("Couldn't read");
  });
  async function confirmButton(label: string) {
    const result = Array.from(document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')).find(b => b.textContent === label);
    if (!result) throw new Error(`Dialog button missing: ${label}`);
    await act(async () => result.click()); await settle();
  }
  it("groups a detail's chunks by document and deletes a whole document with a counted confirm and 10-second undo", async () => {
    const timers = vi.spyOn(globalThis, "setTimeout");
    const del = vi.spyOn(api, "deleteKnowledgeBySource").mockResolvedValue({ enabled: true, deleted: 51 });
    const restore = vi.spyOn(api, "restoreKnowledgeBySource").mockResolvedValue({ enabled: true, restored: 51 });
    await render(); await act(async () => model.open("alpha")); await settle();
    expect(host.textContent).toContain("Manual · 51 chunks");
    const trigger = document.querySelector<HTMLButtonElement>('[aria-label="Delete document Manual"]');
    await act(async () => trigger!.click()); await settle();
    expect(document.body.textContent).toContain("This removes all 51 chunks of “Manual”");
    await confirmButton("Delete document");
    expect(del).toHaveBeenCalledWith("Manual");
    expect(document.body.textContent).toContain("Deleted 51 chunks from “Manual”.");
    expect(timers).toHaveBeenCalledWith(expect.any(Function), 10000);
    await click("Undo"); expect(restore).toHaveBeenCalledWith("Manual");
    expect(document.body.textContent).toContain("Restored “Manual”.");
  });
  it("does not announce a document deletion when the server removed zero chunks", async () => {
    vi.spyOn(api, "deleteKnowledgeBySource").mockResolvedValue({ enabled: true, deleted: 0 });
    await render(); await act(async () => model.open("alpha")); await settle();
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Delete document Manual"]')!.click()); await settle();
    await confirmButton("Delete document");
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain("Couldn't delete “Manual”. Nothing was removed.");
    expect(document.body.textContent).not.toContain("Deleted");
  });
  it("offers no document delete for the shared console bucket or sourceless entries", async () => {
    // delete-by-source has no collection predicate and operator chunks all share source
    // "console", so those buckets span collections and must NOT get a per-document delete.
    const bySource = vi.spyOn(api, "deleteKnowledgeBySource");
    vi.mocked(api.knowledgeSearch).mockResolvedValue({ enabled: true, query: "", results: [{ ...chunk(1), source: "console" }, { ...chunk(2), source: null }], stats: {} });
    await render(); await act(async () => model.open("alpha")); await settle();
    expect(host.textContent).toContain("Entries without a source");
    expect(document.querySelector('[aria-label^="Delete document"]')).toBeNull();
    expect(bySource).not.toHaveBeenCalled();
  });
  it("scopes the detail read to the pending-review verdict and shows a review-specific empty state", async () => {
    vi.mocked(api.knowledgeSearch).mockResolvedValue({ enabled: true, query: "", results: [], stats: {} });
    await render(); await act(async () => model.open("alpha")); await settle();
    expect(host.textContent).toContain("This collection is empty.");
    await act(async () => model.setPendingOnly(true)); await settle();
    expect(api.knowledgeSearch).toHaveBeenCalledWith("", expect.objectContaining({ collection: "alpha", reviewState: "pending" }));
    expect(host.textContent).toContain("Nothing awaiting review here.");
    expect(host.textContent).not.toContain("This collection is empty.");
  });
  it("never sends a private bulk delete for a shared collection", async () => {
    const remove = vi.spyOn(api, "deleteKnowledgeByCollection");
    vi.mocked(api.knowledgeSearch).mockResolvedValue({ enabled: true, query: "", results: [{ ...chunk(1), tier: "commons" }], stats: {} });
    await render();
    await act(async () => { await model.remove.mutateAsync("alpha").catch(() => {}); });
    await settle();
    expect(remove).not.toHaveBeenCalled();
    expect(model.remove.error?.message).toContain("contains shared chunks");
  });
  it("posts collection names intact to the delete and restore routes", async () => {
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ enabled: true, deleted: 3 })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ enabled: true, restored: 3 })));
    const collection = "docs/a?b&c";
    expect((await api.deleteKnowledgeByCollection(collection)).deleted).toBe(3);
    expect((await api.restoreKnowledgeByCollection(collection)).restored).toBe(3);
    expect(fetch).toHaveBeenNthCalledWith(1, expect.stringContaining("/api/knowledge/delete-by-collection"), expect.objectContaining({ method: "POST", body: JSON.stringify({ collection }) }));
    expect(fetch).toHaveBeenNthCalledWith(2, expect.stringContaining("/api/knowledge/restore-by-collection"), expect.objectContaining({ method: "POST", body: JSON.stringify({ collection }) }));
  });
  it("highlights literal search text without interpreting regex or HTML", async () => {
    await act(async () => root.render(<div>{highlight("Read [a] <script> safely", "[a]")}</div>));
    expect(host.querySelector("mark")?.textContent).toBe("[a]");
    expect(host.querySelector("script")).toBeNull();
    expect(host.textContent).toContain("<script>");
  });
  it("renders ingesting and shared metadata without exposing a private delete as unsharing", async () => {
    vi.mocked(api.listCollections).mockResolvedValue({ enabled: true, collections: [{ ...rows[0], tier: "commons", ingesting: { completed: 3, total: 12 } } as Collection] });
    await render(); expect(host.textContent).toContain("Adding 3 of 12 documents"); expect(host.textContent).toContain("shared");
    await act(async () => model.open("alpha")); await settle(); expect(button("Stop sharing…").disabled).toBe(true);
  });
});
