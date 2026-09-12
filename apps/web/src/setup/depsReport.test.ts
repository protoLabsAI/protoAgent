// #3450 — the report has to see BUNDLED plugins, which have no /api/plugins/installed
// row. The Cowork archetype's pack is exactly that shape, and a report that can't see it
// let a fresh server finish setup "ready" with no document libraries installed.
import { describe, expect, it } from "vitest";

import { mergeDeps, needyPlugins } from "./depsReport";

describe("mergeDeps", () => {
  it("unions the lists in first-seen order, deduped", () => {
    expect(mergeDeps(["httpx"], ["httpx", "pillow"])).toEqual(["httpx", "pillow"]);
  });

  it("tolerates absent lists (a bundled plugin has no inventory row; an older backend has no runtime field)", () => {
    expect(mergeDeps(undefined, ["pypdf"])).toEqual(["pypdf"]);
    expect(mergeDeps(null, undefined)).toEqual([]);
  });
});

describe("needyPlugins", () => {
  it("reports a bundled plugin from the runtime meta alone", () => {
    expect(
      needyPlugins(["cowork"], [], [{ id: "cowork", name: "Cowork", deps_missing: ["openpyxl", "python-docx"] }]),
    ).toEqual([{ id: "cowork", name: "Cowork", deps: ["openpyxl", "python-docx"] }]);
  });

  it("still reports a git-installed plugin, preferring its manifest name", () => {
    const rows = needyPlugins(
      ["github"],
      [{ id: "github", deps_missing: ["httpx"], manifest: { name: "GitHub" } }],
      [{ id: "github", name: "github", deps_missing: ["httpx"] }],
    );
    expect(rows).toEqual([{ id: "github", name: "GitHub", deps: ["httpx"] }]);
  });

  it("merges a git plugin's required names (inventory) with its optional ones (runtime)", () => {
    // The inventory reports the required tier only; the first cut let a non-empty
    // inventory list win outright and silently dropped the optional names.
    const rows = needyPlugins(
      ["gp"],
      [{ id: "gp", deps_missing: ["requests"], manifest: { name: "GP" } }],
      [{ id: "gp", deps_missing: ["requests", "pillow"] }],
    );
    expect(rows).toEqual([{ id: "gp", name: "GP", deps: ["requests", "pillow"] }]);
  });

  it("skips plugins with nothing missing, and plugins this run didn't enable", () => {
    expect(
      needyPlugins(
        ["notes"],
        [{ id: "notes", deps_missing: [], manifest: { name: "Notes" } }],
        [
          { id: "notes", deps_missing: [] },
          { id: "cowork", name: "Cowork", deps_missing: ["reportlab"] }, // enabled earlier, not now
        ],
      ),
    ).toEqual([]);
  });

  it("keeps enable order and never reports an id twice", () => {
    const rows = needyPlugins(
      ["cowork", "github", "cowork"],
      [{ id: "github", deps_missing: ["httpx"], manifest: { name: "GitHub" } }],
      [{ id: "cowork", name: "Cowork", deps_missing: ["pypdf"] }],
    );
    expect(rows.map((r) => r.id)).toEqual(["cowork", "github"]);
  });

  it("tolerates a missing/absent deps field on either source (older backend)", () => {
    expect(needyPlugins(["x"], [{ id: "x" }], [{ id: "x" }])).toEqual([]);
    expect(needyPlugins(["x"], [], [])).toEqual([]);
  });
});
