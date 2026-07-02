import { describe, expect, it } from "vitest";

import { findSlashCommand } from "../ext/slashRegistry";
import "./coreSlashCommands"; // side-effect: registers /new, /clear, /effort

import type { SlashContext } from "../ext/slashRegistry";

function ctx(over: Partial<SlashContext> = {}): SlashContext {
  return { rest: "", sessionId: null, noteToThread: () => {}, setDraft: () => {}, focusComposer: () => {}, ...over };
}

describe("core slash commands (dogfood the seam, ADR 0061)", () => {
  it("registers /new, /clear, /effort, /compact through the same registry a fork uses", () => {
    expect(findSlashCommand("new")).toBeTruthy();
    expect(findSlashCommand("clear")).toBeTruthy();
    expect(findSlashCommand("effort")).toBeTruthy();
    expect(findSlashCommand("compact")).toBeTruthy();
  });

  it("/compact is tagged with the chat.compact developer flag (ADR 0068)", () => {
    // Registration is unconditional; the HOST (ChatSurface) hides + skips dispatch of a
    // flag-tagged command while its flag is off. The tag is the contract under test here.
    expect(findSlashCommand("compact")!.flag).toBe("chat.compact");
    expect(findSlashCommand("new")!.flag).toBeUndefined(); // shipped commands stay untagged
  });

  it("/clear, /effort, /compact are no-ops (return false → fall through) without a session", () => {
    expect(findSlashCommand("clear")!.run(ctx())).toBe(false);
    expect(findSlashCommand("effort")!.run(ctx())).toBe(false);
    expect(findSlashCommand("compact")!.run(ctx())).toBe(false);
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
