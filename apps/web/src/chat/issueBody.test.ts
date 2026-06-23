import { describe, expect, it } from "vitest";

import { buildBody, EMPTY_FIELDS, type Fields, isComplete } from "./issueBody";

const FULL: Fields = {
  problem: "the wheel does nothing in the modal",
  repro: "open modal, scroll",
  expected: "expected scroll; got nothing",
  proposal: "wire onWheel to the scroll container",
  acceptance: "modal body scrolls",
  refs: "#1300",
};

describe("buildBody", () => {
  it("emits the bug sections the gate requires", () => {
    const body = buildBody("bug", FULL);
    expect(body).toContain("## Problem");
    expect(body).toContain("## Steps to reproduce / evidence");
    expect(body).toContain("## Expected vs. actual");
    expect(body).toContain("## Acceptance");
    expect(body).not.toContain("## Proposed direction");
  });

  it("emits the feature sections the gate requires", () => {
    const body = buildBody("feature", FULL);
    expect(body).toContain("## Problem");
    expect(body).toContain("## Proposed direction");
    expect(body).toContain("## Acceptance");
    expect(body).not.toContain("## Steps to reproduce");
  });

  it("includes Refs only when provided", () => {
    expect(buildBody("bug", FULL)).toContain("## Refs");
    expect(buildBody("bug", { ...FULL, refs: "  " })).not.toContain("## Refs");
  });

  it("clears the gate's 80-char floor", () => {
    const collapsed = buildBody("feature", FULL).replace(/\s+/g, " ").trim();
    expect(collapsed.length).toBeGreaterThanOrEqual(80);
  });
});

describe("isComplete", () => {
  it("requires title, repo, problem and acceptance", () => {
    expect(isComplete("bug", "", "o/r", FULL)).toBe(false);
    expect(isComplete("bug", "t", "", FULL)).toBe(false);
    expect(isComplete("bug", "t", "o/r", { ...FULL, problem: "" })).toBe(false);
    expect(isComplete("bug", "t", "o/r", { ...FULL, acceptance: "" })).toBe(false);
  });

  it("requires repro + expected for a bug, proposal for a feature", () => {
    expect(isComplete("bug", "t", "o/r", { ...EMPTY_FIELDS, problem: "p", acceptance: "a", repro: "r" })).toBe(false);
    expect(
      isComplete("bug", "t", "o/r", { ...EMPTY_FIELDS, problem: "p", acceptance: "a", repro: "r", expected: "e" }),
    ).toBe(true);
    expect(isComplete("feature", "t", "o/r", { ...EMPTY_FIELDS, problem: "p", acceptance: "a" })).toBe(false);
    expect(isComplete("feature", "t", "o/r", { ...EMPTY_FIELDS, problem: "p", acceptance: "a", proposal: "x" })).toBe(
      true,
    );
  });
});
