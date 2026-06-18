import { describe, expect, it } from "vitest";

import { catalogCategories, filterCatalog } from "./catalog";
import type { CatalogPlugin } from "../lib/types";

const mk = (over: Partial<CatalogPlugin>): CatalogPlugin => ({
  id: "x",
  name: "X",
  repo: "https://github.com/o/x",
  bundled: false,
  installed: false,
  enabled: false,
  ...over,
});

const CAT: CatalogPlugin[] = [
  mk({ id: "discord", name: "Discord", category: "Communication", tagline: "chat bot" }),
  mk({ id: "artifact", name: "Artifact", category: "Generative UI", tagline: "sandboxed iframe" }),
  mk({ id: "pm", name: "Product Manager", category: "Product", tagline: "PM skills + brain" }),
];

describe("filterCatalog", () => {
  it("returns everything for empty query + All", () => {
    expect(filterCatalog(CAT, "", "All")).toHaveLength(3);
  });

  it("matches name, tagline, and id (case-insensitive)", () => {
    expect(filterCatalog(CAT, "DISCORD", "All").map((p) => p.id)).toEqual(["discord"]);
    expect(filterCatalog(CAT, "iframe", "All").map((p) => p.id)).toEqual(["artifact"]);
    expect(filterCatalog(CAT, "pm", "All").map((p) => p.id)).toEqual(["pm"]);
  });

  it("filters by category and combines with query", () => {
    expect(filterCatalog(CAT, "", "Product").map((p) => p.id)).toEqual(["pm"]);
    expect(filterCatalog(CAT, "chat", "Communication").map((p) => p.id)).toEqual(["discord"]);
    expect(filterCatalog(CAT, "chat", "Product")).toEqual([]);
  });
});

describe("catalogCategories", () => {
  it("is All + the distinct categories, sorted", () => {
    expect(catalogCategories(CAT)).toEqual(["All", "Communication", "Generative UI", "Product"]);
  });
});
