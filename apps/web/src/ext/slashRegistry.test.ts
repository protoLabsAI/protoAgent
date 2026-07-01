import { describe, expect, it } from "vitest";

import {
  findSlashCommand,
  registerSlashCommand,
  registeredSlashCommands,
  slashCommandName,
  slashTokenAt,
} from "./slashRegistry";

describe("slash-command registry (ADR 0061)", () => {
  it("registers and finds a command, case-insensitively", () => {
    registerSlashCommand({ name: "Foo", description: "a foo", run: () => true });
    expect(findSlashCommand("foo")?.description).toBe("a foo");
    expect(findSlashCommand("FOO")).toBeTruthy(); // token matched case-insensitively
    expect(registeredSlashCommands().some((c) => c.name === "foo")).toBe(true);
  });

  it("first registration of a token wins (HMR-safe)", () => {
    registerSlashCommand({ name: "dup", description: "first", run: () => true });
    registerSlashCommand({ name: "dup", description: "second", run: () => true });
    expect(findSlashCommand("dup")?.description).toBe("first");
  });

  it("ignores invalid registrations (no name / no run)", () => {
    registerSlashCommand({ name: "", description: "x", run: () => true });
    // @ts-expect-error — missing run
    registerSlashCommand({ name: "norun", description: "x" });
    expect(findSlashCommand("")).toBeUndefined();
    expect(findSlashCommand("norun")).toBeUndefined();
  });

  it("a command's run() controls interception via its boolean return", () => {
    let got = "";
    registerSlashCommand({
      name: "echo",
      description: "echo rest",
      run: (ctx) => {
        got = ctx.rest;
        return true;
      },
    });
    const handled = findSlashCommand("echo")!.run({
      rest: "hello world",
      sessionId: null,
      noteToThread: () => {},
      setDraft: () => {},
      focusComposer: () => {},
    });
    expect(handled).toBe(true);
    expect(got).toBe("hello world");
  });
});

describe("slashTokenAt — MID-INPUT popover parsing (#1530)", () => {
  it("parses a token at the start of the input", () => {
    expect(slashTokenAt("/eff", 4)).toEqual({ query: "eff", start: 0, end: 4 });
  });

  it("parses a token MID-INPUT (after whitespace), not only at char 0", () => {
    // caret right after "/eff" inside "hey /eff"
    expect(slashTokenAt("hey /eff", 8)).toEqual({ query: "eff", start: 4, end: 8 });
  });

  it("uses the caret for query, but end runs to the token's end (mid-token completion)", () => {
    // caret after "/ne" in "/newthing": query is the text BEFORE the caret ("ne"), but end
    // spans the WHOLE token (index 9), so completing replaces "/newthing" with no tail left.
    expect(slashTokenAt("/newthing", 3)).toEqual({ query: "ne", start: 0, end: 9 });
  });

  it("returns the bare token ('/') so the empty query opens the full list", () => {
    expect(slashTokenAt("/", 1)).toEqual({ query: "", start: 0, end: 1 });
    expect(slashTokenAt("hi /", 4)).toEqual({ query: "", start: 3, end: 4 });
  });

  it("is null when the caret is not inside a slash token", () => {
    expect(slashTokenAt("hello", 5)).toBeNull(); // no slash
    expect(slashTokenAt("/eff done", 9)).toBeNull(); // caret past the space → token closed
    expect(slashTokenAt("path/to/file", 12)).toBeNull(); // mid-word slash, not a token start
  });

  it("clamps an out-of-range caret", () => {
    expect(slashTokenAt("/eff", 99)).toEqual({ query: "eff", start: 0, end: 4 });
    expect(slashTokenAt("/eff", -1)).toEqual({ query: "", start: 0, end: 4 });
  });
});

describe("slashCommandName — distinct command bubble detection (#1529)", () => {
  it("names a bare command and a command with args", () => {
    expect(slashCommandName("/goal")).toBe("goal");
    expect(slashCommandName("/goal ship it")).toBe("goal");
    expect(slashCommandName("/effort high")).toBe("effort");
  });

  it("is case-insensitive and tolerates surrounding whitespace", () => {
    expect(slashCommandName("  /New  ")).toBe("New");
  });

  it("does NOT treat a file path or non-command as a command", () => {
    expect(slashCommandName("/home/user/notes.md is here")).toBeNull();
    expect(slashCommandName("just a message")).toBeNull();
    expect(slashCommandName("/")).toBeNull(); // no letter-led word
    expect(slashCommandName("/123")).toBeNull(); // must start with a letter
  });
});
