import { describe, expect, it } from "vitest";

import { findSlashCommand } from "../ext/slashRegistry";
import "./coreSlashCommands"; // side-effect: registers /new, /clear, /effort

import type { ComposerFormSpec, SlashContext } from "../ext/slashRegistry";
import { REASONING_EFFORTS } from "./chat-store";

function ctx(over: Partial<SlashContext> = {}): SlashContext {
  return { rest: "", sessionId: null, noteToThread: () => {}, setDraft: () => {}, focusComposer: () => {}, ...over };
}

/** The `effort` field schema from a `/effort` picker payload (typed access for the tests). */
function effortField(spec: ComposerFormSpec): { oneOf: { const: string }[]; default?: string } {
  const props = (spec.payload.steps![0].schema as { properties: Record<string, unknown> }).properties;
  return props.effort as { oneOf: { const: string }[]; default?: string };
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

describe("/effort composer-form picker (#1701)", () => {
  it("bare /effort opens a picker form with a card for every level", () => {
    let spec: ComposerFormSpec | null = null;
    const handled = findSlashCommand("effort")!.run(ctx({ sessionId: "s1", openForm: (s) => (spec = s) }));
    expect(handled).toBe(true);
    expect(spec).toBeTruthy();
    expect(spec!.payload.kind).toBe("form");
    // A card per level, in order, and a default preselected (the tab's current level).
    expect(effortField(spec!).oneOf.map((o) => o.const)).toEqual([...REASONING_EFFORTS]);
    expect(effortField(spec!).default).toBeTruthy();
  });

  it("submitting the picker applies + notes a valid level; ignores an invalid one", () => {
    let spec: ComposerFormSpec | null = null;
    let noted = "";
    findSlashCommand("effort")!.run(ctx({ sessionId: "s1", openForm: (s) => (spec = s), noteToThread: (m) => (noted = m) }));
    spec!.onSubmit({ effort: "max" });
    expect(noted).toContain("set to **max**");
    noted = "";
    spec!.onSubmit({ effort: "bogus" }); // not a real level → no-op
    expect(noted).toBe("");
  });

  it("falls back to a note when the host hasn't wired openForm (optional seam)", () => {
    let noted = "";
    const handled = findSlashCommand("effort")!.run(ctx({ sessionId: "s1", noteToThread: (m) => (noted = m) }));
    expect(handled).toBe(true);
    expect(noted).toContain("Reasoning effort:");
  });

  it("typed /effort <level> still applies directly, never opening the form", () => {
    let noted = "";
    let opened = false;
    const handled = findSlashCommand("effort")!.run(
      ctx({ sessionId: "s1", rest: "high", noteToThread: (m) => (noted = m), openForm: () => (opened = true) }),
    );
    expect(handled).toBe(true);
    expect(opened).toBe(false);
    expect(noted).toContain("set to **high**");
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
