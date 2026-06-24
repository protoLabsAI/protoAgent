import { describe, expect, it } from "vitest";

import { findSlashCommand, registerSlashCommand, registeredSlashCommands } from "./slashRegistry";

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
