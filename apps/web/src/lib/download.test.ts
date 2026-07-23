import { describe, expect, it } from "vitest";

import { safeFilename } from "./download";

describe("safeFilename", () => {
  it("keeps a readable stem", () => {
    expect(safeFilename("My Merck chat")).toBe("My Merck chat");
  });
  it("strips path-hostile characters", () => {
    expect(safeFilename("re: prod/incident?")).toBe("re- prod-incident");
  });
  it("collapses whitespace and trims edge dots/dashes", () => {
    expect(safeFilename("  ..a   b..  ")).toBe("a b");
  });
  it("falls back when nothing survives", () => {
    expect(safeFilename("///")).toBe("chat");
    expect(safeFilename("")).toBe("chat");
  });
});
