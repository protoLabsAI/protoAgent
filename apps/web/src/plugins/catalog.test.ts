import { describe, expect, it } from "vitest";

import { catalogCardState, catalogCategories, filterCatalog } from "./catalog";
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

  it("matches what a plugin adds, as the website search does (#2910)", () => {
    const withAdds = [
      mk({ id: "doom", name: "DOOM", tagline: "play it", adds: ["view"] }),
      mk({ id: "discord", name: "Discord", tagline: "chat bot", adds: ["surface", "tool"] }),
      mk({ id: "bare", name: "Bare", tagline: "a fork's entry with no adds" }),
    ];
    expect(filterCatalog(withAdds, "view", "All").map((p) => p.id)).toEqual(["doom"]);
    expect(filterCatalog(withAdds, "surface", "All").map((p) => p.id)).toEqual(["discord"]);
    expect(filterCatalog(withAdds, "", "All")).toHaveLength(3);
  });

  it("filters by category and combines with query", () => {
    expect(filterCatalog(CAT, "", "Product").map((p) => p.id)).toEqual(["pm"]);
    expect(filterCatalog(CAT, "chat", "Communication").map((p) => p.id)).toEqual(["discord"]);
    expect(filterCatalog(CAT, "chat", "Product")).toEqual([]);
  });
});

describe("catalogCardState (the Discover card's action slot)", () => {
  it("never offers Install for a plugin that ships in core, whether it's on or off", () => {
    expect(catalogCardState(mk({ bundled: true, enabled: true })).kind).toBe("state");
    expect(catalogCardState(mk({ bundled: true, enabled: false })).kind).toBe("state");
    // A leftover superseded git copy can make a bundled row "installed" too: still no Install.
    expect(catalogCardState(mk({ bundled: true, installed: true, enabled: true })).kind).toBe("state");
  });

  it("shows a bundled plugin's real on/off state, not a bare 'bundled'", () => {
    expect(catalogCardState(mk({ name: "Cowork", bundled: true, enabled: true }))).toEqual({
      kind: "state",
      label: "bundled · on",
      tone: "success",
      title: "Cowork ships with protoAgent and is on.",
      why: undefined,
    });
    expect(catalogCardState(mk({ name: "Telegram", bundled: true, enabled: false }))).toMatchObject({
      label: "bundled · off",
      tone: "muted",
    });
  });

  it("says why a bundled plugin is on when another plugin's enables: turned it on (#3450)", () => {
    const s = catalogCardState(mk({ name: "Execute Code", bundled: true, enabled: true, enabled_by: ["cowork"] }));
    expect(s).toMatchObject({ kind: "state", label: "bundled · on", why: "on because cowork enables it" });
    expect(catalogCardState(mk({ bundled: true, enabled: true, enabled_by: ["a", "b"] }))).toMatchObject({
      why: "on because a, b enable it",
    });
    // An off plugin never claims a reason to be on (older backends may send a stale list).
    expect(catalogCardState(mk({ bundled: true, enabled: false, enabled_by: ["cowork"] }))).not.toHaveProperty("why");
  });

  it("offers Install for a git-installable plugin, and says on/off once it's installed", () => {
    expect(catalogCardState(mk({}))).toEqual({ kind: "install" });
    expect(catalogCardState(mk({ installed: true, enabled: true }))).toMatchObject({ label: "installed · on", tone: "success" });
    expect(catalogCardState(mk({ installed: true, enabled: false }))).toMatchObject({ label: "installed · off", tone: "muted" });
  });
});

describe("catalogCategories", () => {
  it("is All + the distinct categories, sorted", () => {
    expect(catalogCategories(CAT)).toEqual(["All", "Communication", "Generative UI", "Product"]);
  });
});
