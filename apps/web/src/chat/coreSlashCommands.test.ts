import { describe, expect, it } from "vitest";

import { findSlashCommand } from "../ext/slashRegistry";
import "./coreSlashCommands"; // side-effect: registers /new, /clear, /close, /effort

import type { SlashContext } from "../ext/slashRegistry";

function ctx(over: Partial<SlashContext> = {}): SlashContext {
  return { rest: "", sessionId: null, noteToThread: () => {}, setDraft: () => {}, focusComposer: () => {}, ...over };
}

describe("core slash commands (dogfood the seam, ADR 0061)", () => {
  it("registers /new, /clear, /close, /effort through the same registry a fork uses", () => {
    expect(findSlashCommand("new")).toBeTruthy();
    expect(findSlashCommand("clear")).toBeTruthy();
    expect(findSlashCommand("close")).toBeTruthy();
    expect(findSlashCommand("effort")).toBeTruthy();
  });

  it("/clear, /close and /effort are no-ops (return false → fall through) without a session", () => {
    expect(findSlashCommand("clear")!.run(ctx())).toBe(false);
    expect(findSlashCommand("close")!.run(ctx())).toBe(false);
    expect(findSlashCommand("effort")!.run(ctx())).toBe(false);
  });

  it("/close handles the command (returns true) when there is a session", () => {
    expect(findSlashCommand("close")!.run(ctx({ sessionId: "s1" }))).toBe(true);
  });

  it("/effort with an unknown level notes the error and still handles it", () => {
    let noted = "";
    const handled = findSlashCommand("effort")!.run(
      ctx({ sessionId: "s1", rest: "turbo", noteToThread: (m) => (noted = m) }),
    );
    expect(handled).toBe(true);
    expect(noted).toContain("Unknown effort");
  });
});
