import { describe, expect, it } from "vitest";

import { hasHunks, newLineForOld, splitPatch } from "./diffParse";

const PATCH = [
  "diff --git a/src/a.ts b/src/a.ts",
  "index 1..2 100644",
  "--- a/src/a.ts",
  "+++ b/src/a.ts",
  "@@ -1,3 +1,4 @@",
  " one",
  "-two",
  "+TWO",
  "+2.5",
  " three",
  "diff --git a/old name.md b/new name.md",
  "similarity index 90%",
  "rename from old name.md",
  "rename to new name.md",
  "diff --git a/gone.txt b/gone.txt",
  "deleted file mode 100644",
  "--- a/gone.txt",
  "+++ /dev/null",
  "@@ -1 +0,0 @@",
  "-bye",
  "diff --git a/logo.png b/logo.png",
  "Binary files a/logo.png and b/logo.png differ",
  "",
].join("\n");

describe("splitPatch", () => {
  const files = splitPatch(PATCH);

  it("splits one section per file", () => {
    expect(files.map((f) => f.path)).toEqual(["src/a.ts", "new name.md", "gone.txt", "logo.png"]);
  });
  it("counts +/- inside hunks only (not the ---/+++ headers)", () => {
    expect(files[0]).toMatchObject({ additions: 2, deletions: 1, binary: false });
    expect(files[2]).toMatchObject({ additions: 0, deletions: 1 });
  });
  it("tracks renames, deletions and binaries", () => {
    expect(files[1]).toMatchObject({ path: "new name.md", oldPath: "old name.md" });
    expect(files[2].path).toBe("gone.txt");
    expect(files[3].binary).toBe(true);
  });
  it("each section is a standalone patch ending in one newline", () => {
    expect(files[0].patch.startsWith("diff --git a/src/a.ts")).toBe(true);
    expect(files[0].patch.endsWith(" three\n")).toBe(true);
  });
  it("decodes git's C-quoted paths", () => {
    const [f] = splitPatch('diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n--- "a/caf\\303\\251.txt"\n+++ "b/caf\\303\\251.txt"\n@@ -1 +1 @@\n-a\n+b\n');
    expect(f.path).toBe("café.txt");
  });
  it("is empty for nothing", () => {
    expect(splitPatch("")).toEqual([]);
    expect(splitPatch("not a diff")).toEqual([]);
  });
});

describe("newLineForOld — a deleted line opens the nearest CURRENT line", () => {
  const P = [
    "diff --git a/a.ts b/a.ts",
    "--- a/a.ts",
    "+++ b/a.ts",
    "@@ -10,5 +10,4 @@",
    " ten",
    "-eleven",
    "-twelve",
    "+ELEVEN",
    " thirteen",
    " fourteen",
    "@@ -40,2 +39,0 @@",
    "-forty",
    "-forty-one",
    "",
  ].join("\n");
  it("maps a deletion to the new-side line at its position", () => {
    expect(newLineForOld(P, 11)).toBe(11);
    expect(newLineForOld(P, 12)).toBe(11); // still before the replacement line
  });
  it("handles a pure-deletion hunk (+N,0 names the line before the gap)", () => {
    expect(newLineForOld(P, 40)).toBe(40);
    expect(newLineForOld(P, 41)).toBe(40);
  });
  it("is null for a line that isn't a deletion", () => {
    expect(newLineForOld(P, 10)).toBeNull();
    expect(newLineForOld(P, 99)).toBeNull();
  });
  it("hasHunks: a pure rename has none", () => {
    expect(hasHunks(P)).toBe(true);
    expect(hasHunks("diff --git a/x b/y\nsimilarity index 100%\nrename from x\nrename to y\n")).toBe(false);
  });
});
