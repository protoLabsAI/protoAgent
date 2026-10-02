// A trailing markdown marker with nothing after it yet must never paint while a message streams
// (the launch-demo `**` flash): streamdown's remend repairs `**bo` but leaves a bare `**` alone.
import { describe, expect, it } from "vitest";

import { hideDanglingMarker } from "./danglingMarker";

describe("hideDanglingMarker (streaming text only)", () => {
  // [streamed so far, what may paint]
  const cases: Array<[string, string]> = [
    ["**", ""],
    ["*", ""],
    ["***", ""],
    ["_", ""],
    ["__", ""],
    ["~~", ""],
    ["`", ""],
    ["```", ""],
    ["[", ""],
    ["![", ""],
    ["- ", ""],
    ["-", ""],
    ["* ", ""],
    ["1. ", ""],
    ["12) ", ""],
    ["## ", ""],
    ["> ", ""],
    ["Done — appended to your notes:\n\n- **", "Done — appended to your notes:\n\n"],
    ["Done — appended to your notes:\n\n- ", "Done — appended to your notes:\n\n"],
    ["Done:\n\n1. ", "Done:\n\n"],
    ["Done: **", "Done:"],
    ["pinned in `", "pinned in"],
    ["it is ~~", "it is"],
    ["see (**", "see ("],
    ["Done:\n-", "Done:\n"], // a setext underline would turn "Done:" into a heading
    ["Done:\n==", "Done:\n"],
    ["- **protoAgent** — private\n- ", "- **protoAgent** — private\n"],
  ];
  for (const [input, want] of cases) {
    it(`hides the dangling tail of ${JSON.stringify(input)}`, () => {
      expect(hideDanglingMarker(input)).toBe(want);
    });
  }

  // Text whose tail already has content — or is a CLOSING marker — is left exactly as is
  // (remend closes the open ones).
  const untouched = [
    "",
    "Done",
    "**bo",
    "**bold**",
    "- **protoAgent",
    "- item",
    "1. first",
    "`code`",
    "`co",
    "[link",
    "[link](https://x",
    "snake_case",
    "a * b",
    "2 ** 3 = 8",
    "Done:\n\n",
    "x\\*",
  ];
  for (const input of untouched) {
    it(`leaves ${JSON.stringify(input)} alone`, () => {
      expect(hideDanglingMarker(input)).toBe(input);
    });
  }

  it("never touches the tail inside an open fenced code block", () => {
    const code = "Here:\n\n```py\nx = a **";
    expect(hideDanglingMarker(code)).toBe(code);
    expect(hideDanglingMarker("```\n- ")).toBe("```\n- ");
  });

  it("hides a fence that has just opened but resumes once the fence is closed", () => {
    expect(hideDanglingMarker("Here:\n\n```")).toBe("Here:\n\n");
    expect(hideDanglingMarker("```\ncode\n```\n**")).toBe("```\ncode\n```\n");
  });
});
