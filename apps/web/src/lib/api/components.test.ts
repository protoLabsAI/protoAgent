import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The component-v1 catalog slice (ADR 0118 D5, S11b `GET /api/components`): fetch-once caching,
// the frame-url resolver, and cache invalidation on a plugin (re)load. `onTopic` is mocked so
// the module's plugin watch doesn't open the real SSE stream; `fetch` is stubbed to serve the
// catalog, exercising the real `request` path.
//
// The module subscribes to `plugin.#` exactly ONCE (lazily, on the first fetch), so we capture
// the handler here and never null it — it stays the live invalidation hook for the whole file.
let pluginReload: (() => void) | null = null;
vi.mock("../events", () => ({
  onTopic: (_pattern: string, fn: () => void) => {
    pluginReload = fn;
    return () => {};
  },
}));

import {
  fetchComponentCatalog,
  frameUrlFor,
  refreshComponentCatalog,
  type ComponentCatalogEntry,
} from "./components";

const CATALOG: ComponentCatalogEntry[] = [
  { name: "code-ref", plugin: null, frame_url: null },
  { name: "board-card", plugin: "projectBoard", frame_url: "/plugins/projectBoard/card" },
];

function stubFetch(body: unknown) {
  // A FRESH Response per call — a Response body can only be read once, so a shared instance
  // would throw "Body is unusable" on the second fetch (the plugin-reload re-fetch).
  return vi.spyOn(globalThis, "fetch").mockImplementation(() =>
    Promise.resolve(
      new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }),
    ),
  );
}

beforeEach(() => {
  refreshComponentCatalog(); // drop any cache carried from a previous test
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("fetchComponentCatalog", () => {
  it("fetches once and serves the cached rows on subsequent calls", async () => {
    const fetchSpy = stubFetch(CATALOG);
    const first = await fetchComponentCatalog();
    const second = await fetchComponentCatalog();
    expect(first).toEqual(CATALOG);
    expect(second).toBe(first); // same cached promise result
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("re-fetches after a plugin reload drops the cache", async () => {
    const fetchSpy = stubFetch(CATALOG);
    await fetchComponentCatalog();
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    // A `plugin.#` frame invalidates the cache; the next read hits the network again.
    expect(pluginReload).toBeTypeOf("function");
    pluginReload!();
    await fetchComponentCatalog();
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it("clears the cache on a failed fetch so the next call retries", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(CATALOG), { status: 200, headers: { "Content-Type": "application/json" } }),
      );
    await expect(fetchComponentCatalog()).rejects.toThrow();
    const rows = await fetchComponentCatalog();
    expect(rows).toEqual(CATALOG);
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });
});

describe("frameUrlFor", () => {
  it("resolves a frame kind's url, and null for a core / frame-less / unknown name", () => {
    expect(frameUrlFor("board-card", CATALOG)).toBe("/plugins/projectBoard/card");
    expect(frameUrlFor("code-ref", CATALOG)).toBeNull();
    expect(frameUrlFor("nope", CATALOG)).toBeNull();
  });
});
