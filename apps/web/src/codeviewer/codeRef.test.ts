import { describe, expect, it } from "vitest";

import { codeRefFromProps, refLabel } from "./codeRef";

describe("codeRefFromProps", () => {
  it("reads show_code's props", () => {
    expect(codeRefFromProps({ project: "p", path: "a.ts", line: 3, end_line: 9, note: "why" })).toEqual({
      project: "p",
      path: "a.ts",
      line: 3,
      endLine: 9,
      note: "why",
    });
  });
  it("never trusts the wire: wrong types and bad ranges drop out", () => {
    expect(codeRefFromProps({ project: 1, path: "a" })).toBeNull();
    expect(codeRefFromProps({ project: "p", path: "a", line: "3", end_line: 9 })).toMatchObject({
      line: undefined,
      endLine: undefined,
    });
    expect(codeRefFromProps({ project: "p", path: "a", line: 5, end_line: 2 })).toMatchObject({ line: 5, endLine: undefined });
    expect(codeRefFromProps(undefined)).toBeNull();
  });
});

describe("refLabel", () => {
  it("formats path, path:line, path:a-b", () => {
    expect(refLabel({ path: "a.ts" })).toBe("a.ts");
    expect(refLabel({ path: "a.ts", line: 3 })).toBe("a.ts:3");
    expect(refLabel({ path: "a.ts", line: 3, endLine: 7 })).toBe("a.ts:3-7");
  });
});
