import { describe, expect, it } from "vitest";

import { continueRun, leadingRun, runHas, toggleMention } from "./mentionRun";

describe("leadingRun", () => {
  it("splits run from body", () => {
    expect(leadingRun("@proto @reviewer fix it")).toEqual({ names: ["proto", "reviewer"], body: "fix it" });
  });
  it("no run means all body", () => {
    expect(leadingRun("fix it @proto")).toEqual({ names: [], body: "fix it @proto" });
  });
  it("a bare run has an empty body", () => {
    expect(leadingRun("@proto ")).toEqual({ names: ["proto"], body: "" });
  });
});

describe("toggleMention — what a cast chip click does", () => {
  it("adds to an empty draft", () => {
    expect(toggleMention("", "proto")).toBe("@proto ");
  });
  it("adds a SECOND participant instead of silently doing nothing", () => {
    // The reported bug: chip #2 was a no-op because the handler refused a non-empty draft.
    expect(toggleMention("@proto ", "reviewer")).toBe("@proto @reviewer ");
  });
  it("preserves a message body while editing the run", () => {
    expect(toggleMention("@proto fix the bug", "reviewer")).toBe("@proto @reviewer fix the bug");
  });
  it("clicking an active chip removes just that mention", () => {
    expect(toggleMention("@proto @reviewer fix it", "proto")).toBe("@reviewer fix it");
  });
  it("removing the last mention leaves a clean lead-agent draft", () => {
    expect(toggleMention("@proto fix it", "proto")).toBe("fix it");
  });
  it("is case-insensitive about identity but keeps what the user typed", () => {
    expect(runHas("@Proto hi", "proto")).toBe(true);
    expect(toggleMention("@Proto hi", "proto")).toBe("hi");
  });
});

describe("continueRun — the conversation prefill", () => {
  it("rebuilds the answered run, ready to type", () => {
    expect(continueRun(["proto", "reviewer"])).toBe("@proto @reviewer ");
  });
  it("nothing to continue, nothing prefilled", () => {
    expect(continueRun([])).toBe("");
  });
});
