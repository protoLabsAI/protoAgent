import { describe, expect, it } from "vitest";

import { followRefFromTool } from "./followRef";

describe("followRefFromTool", () => {
  it("read_file → the read's range", () => {
    expect(followRefFromTool("read_file", '{"project":"p","path":"a.py","offset":40,"limit":10}', "…")).toEqual({
      project: "p",
      path: "a.py",
      line: 40,
      endLine: 49,
    });
    expect(followRefFromTool("read_file", '{"project":"p","path":"a.py"}', "…")).toEqual({
      project: "p",
      path: "a.py",
      line: undefined,
    });
  });
  it("edit_file / write_file → the file", () => {
    expect(followRefFromTool("edit_file", '{"project":"p","path":"a.py","old":"x"}', "Edited a.py.")).toEqual({
      project: "p",
      path: "a.py",
    });
    // A write_file's args preview is cut mid-content — project/path still survive.
    expect(followRefFromTool("write_file", '{"project": "p", "path": "b.md", "content": "lo', "Created")).toEqual({
      project: "p",
      path: "b.md",
    });
  });
  it("search_files → its first HIT (not the searched directory, not a context line)", () => {
    const out = "src/a.py-3- ctx: 9: x\nsrc/b.py:12: needle\nsrc/c.py:4: needle";
    expect(followRefFromTool("search_files", '{"project":"p","path":"src","query":"needle"}', out)).toEqual({
      project: "p",
      path: "src/b.py",
      line: 12,
    });
    expect(followRefFromTool("search_files", '{"project":"p","query":"x"}', "(no matches)")).toBeNull();
  });
  it("ignores other tools and unparseable args", () => {
    expect(followRefFromTool("find_files", '{"project":"p","pattern":"*"}', "a.py")).toBeNull();
    expect(followRefFromTool("read_file", '{"pa', "x")).toBeNull();
  });
});
