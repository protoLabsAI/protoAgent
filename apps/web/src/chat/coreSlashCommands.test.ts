import { describe, expect, it } from "vitest";

import { findSlashCommand } from "../ext/slashRegistry";
import "./coreSlashCommands"; // side-effect: registers /new, /clear, /effort

import type { SlashContext } from "../ext/slashRegistry";

function ctx(over: Partial<SlashContext> = {}): SlashContext {
  return { rest: "", sessionId: null, noteToThread: () => {}, setDraft: () => {}, focusComposer: () => {}, ...over };
}

describe("core slash commands (dogfood the seam, ADR 0061)", () => {
  it("registers /new, /clear, /effort, /compact, /help through the same registry a fork uses", () => {
    expect(findSlashCommand("new")).toBeTruthy();
    expect(findSlashCommand("clear")).toBeTruthy();
    expect(findSlashCommand("effort")).toBeTruthy();
    expect(findSlashCommand("compact")).toBeTruthy();
    expect(findSlashCommand("help")).toBeTruthy();
  });

  it("/compact is tagged with the chat.compact developer flag (ADR 0068)", () => {
    // Registration is unconditional; the HOST (ChatSurface) hides + skips dispatch of a
    // flag-tagged command while its flag is off. The tag is the contract under test here.
    expect(findSlashCommand("compact")!.flag).toBe("chat.compact");
    expect(findSlashCommand("new")!.flag).toBeUndefined(); // shipped commands stay untagged
  });

  it("/clear, /effort, /compact, /help are no-ops (return false → fall through) without a session", () => {
    expect(findSlashCommand("clear")!.run(ctx())).toBe(false);
    expect(findSlashCommand("effort")!.run(ctx())).toBe(false);
    expect(findSlashCommand("compact")!.run(ctx())).toBe(false);
    expect(findSlashCommand("help")!.run(ctx())).toBe(false);
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

describe("/help — the live command/shortcut reference card (#1700)", () => {
  function helpCard(over: Partial<SlashContext> = {}): string {
    let noted = "";
    const handled = findSlashCommand("help")!.run(
      ctx({ sessionId: "s1", noteToThread: (m) => (noted = m), ...over }),
    );
    expect(handled).toBe(true);
    return noted;
  }

  it("enumerates the LIVE registry, not a hardcoded list", () => {
    const card = helpCard();
    for (const name of ["new", "clear", "effort", "incognito", "bypass", "help"]) {
      expect(card).toContain(`\`/${name}\``);
    }
  });

  it("respects the host's flag gate: a flag-tagged command is listed only while its flag is ON", () => {
    // /compact is tagged chat.compact. Fail-closed with no predicate at all…
    expect(helpCard()).not.toContain("`/compact`");
    // …hidden while the flag resolves off…
    expect(helpCard({ flagOn: () => false })).not.toContain("`/compact`");
    // …listed while it resolves on (same predicate the slash menu uses).
    expect(helpCard({ flagOn: (id) => id === "chat.compact" })).toContain("`/compact`");
  });

  it("lists the host's server commands (installed plugins) and dedupes client-claimed tokens", () => {
    const card = helpCard({
      serverCommands: [
        { name: "goal", description: "Set or check goals" },
        { name: "help", description: "a server /help must NOT double-list" },
      ],
    });
    expect(card).toContain("`/goal` — Set or check goals");
    expect(card.match(/`\/help`/g)).toHaveLength(1);
  });

  it("carries the shortcuts the composer placeholder no longer teaches (#1697/#1699)", () => {
    const card = helpCard();
    expect(card).toContain("Shift+click"); // incognito + no-confirm delete gestures
    expect(card).toContain("incognito");
    expect(card).toContain("⌘/Ctrl+Enter"); // moved out of the placeholder
    expect(card).toContain("**Capabilities**");
  });
});
