import { describe, expect, it } from "vitest";

import {
  bundleLabel,
  contributionCount,
  filterInstalled,
  needsAttention,
  sortInstalled,
  statusCounts,
  type InstalledRow,
} from "./installed";

const mk = (
  over: Partial<InstalledRow["p"]> & {
    behind?: boolean;
    depsMissing?: string[];
    bundle?: InstalledRow["bundle"];
  },
): InstalledRow => {
  const { behind = false, depsMissing = [], bundle, ...p } = over;
  return {
    p: { id: "x", name: "X", enabled: true, loaded: true, tools: [], skills: 0, ...p },
    behind,
    depsMissing,
    bundle,
  };
};

const ROWS: InstalledRow[] = [
  mk({ id: "careercoach", name: "Career Coach", tools: ["search_jobs", "tailor_cv"], skills: 3 }),
  mk({ id: "doom", name: "DOOM", loaded: false, enabled: false }),
  mk({ id: "github", name: "GitHub", tools: ["gh_pr_list"], views: [{ id: "v", label: "V", path: "/p" }] }),
  mk({ id: "broken", name: "Broken", error: "boom" }),
  mk({ id: "stale", name: "Stale", behind: true }),
  mk({ id: "nodeps", name: "No Deps", depsMissing: ["httpx"] }),
];

describe("needsAttention", () => {
  it("flags error, incomplete, update-behind, and missing deps — and nothing else", () => {
    expect(needsAttention(mk({}))).toBe(false);
    expect(needsAttention(mk({ error: "boom" }))).toBe(true);
    expect(needsAttention(mk({ incomplete: true }))).toBe(true);
    expect(needsAttention(mk({ behind: true }))).toBe(true);
    expect(needsAttention(mk({ depsMissing: ["httpx"] }))).toBe(true);
  });
});

describe("filterInstalled", () => {
  it("returns everything for empty query + All", () => {
    expect(filterInstalled(ROWS, "", "All")).toHaveLength(ROWS.length);
  });

  it("matches name, id, and TOOL names (case-insensitive)", () => {
    expect(filterInstalled(ROWS, "career", "All").map((r) => r.p.id)).toEqual(["careercoach"]);
    expect(filterInstalled(ROWS, "DOOM", "All").map((r) => r.p.id)).toEqual(["doom"]);
    // which plugin ships tool X — the point of searching tool names
    expect(filterInstalled(ROWS, "tailor_cv", "All").map((r) => r.p.id)).toEqual(["careercoach"]);
  });

  it("filters by status chip", () => {
    expect(filterInstalled(ROWS, "", "Disabled").map((r) => r.p.id)).toEqual(["doom"]);
    expect(filterInstalled(ROWS, "", "Loaded")).toHaveLength(ROWS.length - 1);
    expect(filterInstalled(ROWS, "", "Attention").map((r) => r.p.id).sort()).toEqual(["broken", "nodeps", "stale"]);
  });

  it("combines query and status", () => {
    expect(filterInstalled(ROWS, "doom", "Loaded")).toEqual([]);
  });
});

describe("sortInstalled", () => {
  it("status (default): loaded first, attention floats up, name breaks ties; input not mutated", () => {
    const input = [...ROWS];
    const ids = sortInstalled(ROWS, { key: "status", dir: "asc" }).map((r) => r.p.id);
    expect(ids).toEqual(["broken", "nodeps", "stale", "careercoach", "github", "doom"]);
    expect(ROWS).toEqual(input);
  });

  it("name asc/desc", () => {
    const asc = sortInstalled(ROWS, { key: "name", dir: "asc" }).map((r) => r.p.name);
    expect(asc).toEqual(["Broken", "Career Coach", "DOOM", "GitHub", "No Deps", "Stale"]);
    expect(sortInstalled(ROWS, { key: "name", dir: "desc" }).map((r) => r.p.name)).toEqual([...asc].reverse());
  });

  it("contributions: most first, disabled plugins contribute nothing", () => {
    const ids = sortInstalled(ROWS, { key: "contributions", dir: "asc" }).map((r) => r.p.id);
    expect(ids.slice(0, 2)).toEqual(["careercoach", "github"]); // 5 vs 2
    expect(contributionCount(ROWS[1].p)).toBe(0); // doom is disabled
  });
});

describe("bundle provenance", () => {
  const ROWS_B = [
    mk({ id: "board", name: "Project Board", bundle: { id: "pm_stack", name: "Project Manager" } }),
    mk({ id: "docs2", name: "Docs2", bundle: { id: "old_stack" } }), // pre-name lock
    mk({ id: "solo", name: "Solo" }),
  ];

  it("bundleLabel prefers name, falls back to id, null without a bundle", () => {
    expect(bundleLabel(ROWS_B[0])).toBe("Project Manager");
    expect(bundleLabel(ROWS_B[1])).toBe("old_stack"); // locks written before name was persisted
    expect(bundleLabel(ROWS_B[2])).toBeNull();
  });

  it("search matches bundle name AND id — 'everything this stack installed'", () => {
    expect(filterInstalled(ROWS_B, "project manager", "All").map((r) => r.p.id)).toEqual(["board"]);
    expect(filterInstalled(ROWS_B, "pm_stack", "All").map((r) => r.p.id)).toEqual(["board"]);
    expect(filterInstalled(ROWS_B, "old_stack", "All").map((r) => r.p.id)).toEqual(["docs2"]);
  });
});

describe("statusCounts", () => {
  it("counts every chip", () => {
    expect(statusCounts(ROWS)).toEqual({ All: 6, Loaded: 5, Disabled: 1, Attention: 3 });
  });
});
