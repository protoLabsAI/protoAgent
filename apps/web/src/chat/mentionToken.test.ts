import { describe, expect, it } from "vitest";

import { mentionTokenAt } from "./mentionToken";

describe("mentionTokenAt", () => {
  it("opens on a leading sigil", () => {
    expect(mentionTokenAt("@", 1)).toEqual({ query: "", start: 0, end: 1 });
  });

  it("filters on the typed name", () => {
    expect(mentionTokenAt("@pro", 4)).toEqual({ query: "pro", start: 0, end: 4 });
  });

  it("bounds the token so completing mid-name replaces the whole thing", () => {
    // caret after "@pr", token runs to the whitespace at index 6
    expect(mentionTokenAt("@proto fix it", 3)).toEqual({ query: "pr", start: 0, end: 6 });
  });

  it("closes once the caret is in the message", () => {
    expect(mentionTokenAt("@proto fix it", 10)).toBeNull();
  });

  it("never opens on a mention that is not leading", () => {
    // The dispatcher routes only a LEADING mention — offering this one would suggest a
    // target the message will never reach.
    expect(mentionTokenAt("ask @proto about it", 10)).toBeNull();
  });

  it("never opens on an email address", () => {
    expect(mentionTokenAt("mail josh@protolabs.studio", 20)).toBeNull();
  });

  it("does not open on a slash command", () => {
    expect(mentionTokenAt("/goal ship it", 3)).toBeNull();
  });

  it("rejects a token that is not name-shaped", () => {
    expect(mentionTokenAt("@!!", 3)).toBeNull();
  });

  it("accepts the hyphens and dots delegate names carry", () => {
    expect(mentionTokenAt("@claude-code", 12)?.query).toBe("claude-code");
    expect(mentionTokenAt("@gpt-5.1", 8)?.query).toBe("gpt-5.1");
  });

  it("clamps a caret past the end of the text", () => {
    expect(mentionTokenAt("@proto", 999)).toEqual({ query: "proto", start: 0, end: 6 });
  });
});

describe("a leading RUN of mentions (multi-@)", () => {
  it("opens for a second mention while the first token is a mention", () => {
    // "@proto @rev|" — the dispatcher fans a leading run out, so the popover follows.
    expect(mentionTokenAt("@proto @rev", 11)).toEqual({ query: "rev", start: 7, end: 11 });
  });

  it("opens for a bare second sigil", () => {
    expect(mentionTokenAt("@proto @", 8)).toEqual({ query: "", start: 7, end: 8 });
  });

  it("opens for a third mention too", () => {
    expect(mentionTokenAt("@proto @reviewer @cl", 20)).toEqual({ query: "cl", start: 17, end: 20 });
  });

  it("stays closed once a non-mention token starts the message", () => {
    // "fix" ends the run — an `@` after it is prose, and completing it would suggest
    // a target the message never reaches.
    expect(mentionTokenAt("@proto fix @rev", 15)).toBeNull();
  });

  it("bounds the second token so completing mid-name replaces all of it", () => {
    // caret after "@rev" inside "@reviewer" — end runs to the whitespace
    expect(mentionTokenAt("@proto @reviewer go", 11)).toEqual({ query: "rev", start: 7, end: 16 });
  });

  it("caret in the whitespace between mentions belongs to neither", () => {
    expect(mentionTokenAt("@proto  @reviewer", 7)).toBeNull();
  });

  it("still closes once the caret is in the message body", () => {
    expect(mentionTokenAt("@proto @reviewer fix it", 20)).toBeNull();
  });
});
