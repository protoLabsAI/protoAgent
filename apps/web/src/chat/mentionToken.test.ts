import { describe, expect, it } from "vitest";

import { mentionTokenAt } from "./mentionToken";

// The autocomplete trigger opens for any @-token the caret is in, ANYWHERE (#3042) —
// not only a leading run. Routing (leading-only) is a separate server rule.

describe("mentionTokenAt — leading", () => {
  it("opens on a bare leading sigil", () => {
    expect(mentionTokenAt("@", 1)).toEqual({ query: "", start: 0, end: 1 });
  });
  it("filters on the typed name", () => {
    expect(mentionTokenAt("@pro", 4)).toEqual({ query: "pro", start: 0, end: 4 });
  });
  it("bounds the token so completing mid-name replaces the whole thing", () => {
    expect(mentionTokenAt("@proto fix it", 3)).toEqual({ query: "pr", start: 0, end: 6 });
  });
});

describe("mentionTokenAt — anywhere (the fix)", () => {
  it("opens mid-message, after prose", () => {
    // "hello team, @bo|" — caret in @bo, which starts at index 12.
    expect(mentionTokenAt("hello team, @bo", 15)).toEqual({ query: "bo", start: 12, end: 15 });
  });

  it("opens for a bare @ mid-message", () => {
    expect(mentionTokenAt("hey @", 5)).toEqual({ query: "", start: 4, end: 5 });
  });

  it("opens for the SECOND name in prose (multiples apart)", () => {
    // "@bob and @bil|l should pair" — the second token starts at index 9.
    const t = "@bob and @bill should pair";
    expect(mentionTokenAt(t, 13)).toEqual({ query: "bil", start: 9, end: 14 });
  });

  it("closes once the caret leaves the token (in surrounding prose)", () => {
    expect(mentionTokenAt("hello team, @bob and", 18)).toBeNull(); // caret in "and"
    expect(mentionTokenAt("hello team, @bob and", 11)).toBeNull(); // caret in "team,"
  });
});

describe("mentionTokenAt — non-mentions", () => {
  it("never triggers on an email address (@ is mid-token)", () => {
    expect(mentionTokenAt("mail josh@protolabs.studio", 20)).toBeNull();
    expect(mentionTokenAt("josh@example.com", 10)).toBeNull();
  });
  it("does not trigger on a slash command", () => {
    expect(mentionTokenAt("/goal ship it", 3)).toBeNull();
  });
  it("rejects a token that is not name-shaped", () => {
    expect(mentionTokenAt("@!!", 3)).toBeNull();
  });
  it("accepts the hyphens and dots delegate names carry", () => {
    expect(mentionTokenAt("@claude-code", 12)?.query).toBe("claude-code");
    expect(mentionTokenAt("ping @gpt-5.1", 13)?.query).toBe("gpt-5.1");
  });
  it("clamps a caret past the end of the text", () => {
    expect(mentionTokenAt("@proto", 999)).toEqual({ query: "proto", start: 0, end: 6 });
  });
});
