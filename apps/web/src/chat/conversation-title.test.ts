import { describe, expect, it } from "vitest";

import { conversationMeta, displayTitle } from "./conversation-title";

describe("displayTitle strips a redundant 'From <caller>:' prefix (§3.4 item 2c)", () => {
  it("a. strips 'From <caller> (qualifier): …' for the matching caller", () => {
    expect(
      displayTitle("From Claude Code (Josh's session): Josh's goal…", { caller: "Claude Code" }),
    ).toBe("Josh's goal…");
  });

  it("b. matches the caller case-insensitively", () => {
    expect(
      displayTitle("From Claude Code (Josh's session): Josh's goal…", { caller: "claude code" }),
    ).toBe("Josh's goal…");
  });

  it("c. matches onBehalfOf and capitalises the first remaining letter", () => {
    expect(displayTitle("From Josh: please check…", { caller: "Claude Code", onBehalfOf: "Josh" })).toBe(
      "Please check…",
    );
  });

  it("d. leaves the title unchanged with no caller (undefined or empty)", () => {
    expect(displayTitle("From Claude Code: hello", {})).toBe("From Claude Code: hello");
    expect(displayTitle("From Claude Code: hello", { caller: "" })).toBe("From Claude Code: hello");
    expect(displayTitle("From Claude Code: hello", { caller: undefined })).toBe("From Claude Code: hello");
  });

  it("e. leaves a prefix naming someone else unchanged", () => {
    expect(displayTitle("From brandLaunch: ship it", { caller: "Claude Code" })).toBe(
      "From brandLaunch: ship it",
    );
  });

  it("f. does not strip on a partial-word match", () => {
    expect(displayTitle("From Claude Codex: hello", { caller: "Claude Code" })).toBe(
      "From Claude Codex: hello",
    );
  });

  it("g. leaves it unchanged when more than 60 characters precede the colon", () => {
    const title =
      "From Claude Code and everyone else who was on the very long thread yesterday: do it";
    // Guard the premise: the colon really is past the 60-char ceiling, so the only reason the
    // title survives is the length, not a failed name match.
    expect(title.indexOf(":")).toBeGreaterThan(60);
    expect(displayTitle(title, { caller: "Claude Code" })).toBe(title);
  });

  it("h. keeps the whole title when it is only the prefix", () => {
    expect(displayTitle("From Claude Code: ", { caller: "Claude Code" })).toBe("From Claude Code: ");
  });

  it("i. leaves a title with no 'From' prefix unchanged", () => {
    expect(displayTitle("Josh's goal for the week", { caller: "Claude Code" })).toBe(
      "Josh's goal for the week",
    );
  });

  it("j. matches a caller containing regex characters literally", () => {
    expect(displayTitle("From A.B (x): hello there", { caller: "A.B (x)" })).toBe("Hello there");
    // The escaped "." must match a literal dot only — not an arbitrary character.
    expect(displayTitle("From AxB (x): hello there", { caller: "A.B (x)" })).toBe(
      "From AxB (x): hello there",
    );
  });
});

describe("conversationMeta formats the muted origin · time · count line (§9 C9, criterion 9)", () => {
  // Built from local date parts so the en-GB format round-trips in any runner timezone.
  const WHEN = new Date(2026, 10, 14, 12, 29).toISOString();

  it("with origin fields: drops 'From ' in the text, keeps it in the tooltip long form", () => {
    const meta = conversationMeta("From Claude Code for Josh", WHEN, 14);
    expect(meta.text).toBe("Claude Code for Josh · 14 Nov, 12:29 · 14 messages");
    expect(meta.tooltip).toBe("From Claude Code for Josh · 14 Nov at 12:29 · 14 messages");
  });

  it("with no metadata: 'From another agent' → 'another agent'", () => {
    const meta = conversationMeta("From another agent", WHEN, 3);
    expect(meta.text).toBe("another agent · 14 Nov, 12:29 · 3 messages");
    expect(meta.tooltip).toBe("From another agent · 14 Nov at 12:29 · 3 messages");
  });

  it("omits the count at a single message", () => {
    const meta = conversationMeta("From Claude Code for Josh", WHEN, 1);
    expect(meta.text).toBe("Claude Code for Josh · 14 Nov, 12:29");
    expect(meta.tooltip).toBe("From Claude Code for Josh · 14 Nov at 12:29");
  });

  it("leaves a 'Delegation from …' label whole (only a leading 'From ' is dropped)", () => {
    const meta = conversationMeta("Delegation from protoEngineer", WHEN, 2);
    expect(meta.text).toBe("Delegation from protoEngineer · 14 Nov, 12:29 · 2 messages");
    expect(meta.tooltip).toBe("Delegation from protoEngineer · 14 Nov at 12:29 · 2 messages");
  });

  it("drops the date part when there is no timestamp", () => {
    expect(conversationMeta("From Claude Code", null, 5)).toEqual({
      text: "Claude Code · 5 messages",
      tooltip: "From Claude Code · 5 messages",
    });
  });

  it("reads a numeric timestamp as epoch seconds, like relTime", () => {
    const secs = Math.floor(new Date(2026, 10, 14, 12, 29).getTime() / 1000);
    expect(conversationMeta("From Claude Code", secs, 1).text).toBe("Claude Code · 14 Nov, 12:29");
  });
});
