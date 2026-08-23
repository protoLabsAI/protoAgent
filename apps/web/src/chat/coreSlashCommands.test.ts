import { afterEach, describe, expect, it, vi } from "vitest";

import { findSlashCommand } from "../ext/slashRegistry";
import "./coreSlashCommands"; // side-effect: registers /new, /clear, /effort

import type { ComposerFormSpec, SlashContext } from "../ext/slashRegistry";
import { api } from "../lib/api";
import { chatStore, REASONING_EFFORTS } from "./chat-store";

function ctx(over: Partial<SlashContext> = {}): SlashContext {
  return { rest: "", sessionId: null, noteToThread: () => {}, setDraft: () => {}, focusComposer: () => {}, ...over };
}

/** The `effort` field schema from a `/effort` picker payload (typed access for the tests). */
function effortField(spec: ComposerFormSpec): { oneOf: { const: string }[]; default?: string } {
  const props = (spec.payload.steps![0].schema as { properties: Record<string, unknown> }).properties;
  return props.effort as { oneOf: { const: string }[]; default?: string };
}

afterEach(() => {
  vi.restoreAllMocks(); // undo the /clear spies on chatStore / api
});

describe("core slash commands (dogfood the seam, ADR 0061)", () => {
  it("registers /new, /clear, /effort, /compact, /help through the same registry a fork uses", () => {
    expect(findSlashCommand("new")).toBeTruthy();
    expect(findSlashCommand("clear")).toBeTruthy();
    expect(findSlashCommand("effort")).toBeTruthy();
    expect(findSlashCommand("compact")).toBeTruthy();
    expect(findSlashCommand("help")).toBeTruthy();
  });

  it("/compact is untagged — generally available since #2785 (ADR 0101 D5)", () => {
    // Registration is unconditional; the HOST (ChatSurface) hides + skips dispatch of a
    // flag-tagged command while its flag is off. /publish keeps a tag, so the gating
    // contract still has a live subject; /compact shed its expired dev flag.
    expect(findSlashCommand("compact")!.flag).toBeUndefined();
    expect(findSlashCommand("publish")!.flag).toBe("chat.publish");
    expect(findSlashCommand("new")!.flag).toBeUndefined(); // shipped commands stay untagged
  });

  it("/clear, /effort, /compact, /help are no-ops (return false → fall through) without a session", () => {
    expect(findSlashCommand("clear")!.run(ctx())).toBe(false);
    expect(findSlashCommand("effort")!.run(ctx())).toBe(false);
    expect(findSlashCommand("compact")!.run(ctx())).toBe(false);
    expect(findSlashCommand("help")!.run(ctx())).toBe(false);
  });

  it("/clear parks a clear request for confirmation instead of wiping inline (#2996)", () => {
    const request = vi.spyOn(chatStore, "requestClearSession");
    const del = vi.spyOn(api, "deleteChatSession").mockResolvedValue({ deleted: true, harvested: false });
    const wipe = vi.spyOn(chatStore, "updateMessages");
    let focused = false;

    const handled = findSlashCommand("clear")!.run(
      ctx({ sessionId: "s1", focusComposer: () => (focused = true) }),
    );

    expect(handled).toBe(true); // still claims the /clear token
    expect(request).toHaveBeenCalledWith("s1");
    // The destructive work waits on ChatSurface's confirm dialog — NOT the command.
    expect(del).not.toHaveBeenCalled();
    expect(wipe).not.toHaveBeenCalled();
    expect(focused).toBe(true);
  });

  it("/clear without a session falls through AND parks no request", () => {
    const request = vi.spyOn(chatStore, "requestClearSession");
    expect(findSlashCommand("clear")!.run(ctx())).toBe(false);
    expect(request).not.toHaveBeenCalled();
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
    // /publish is tagged chat.publish (/compact shed its flag in #2785). Fail-closed
    // with no predicate at all…
    expect(helpCard()).not.toContain("`/publish`");
    // …hidden while the flag resolves off…
    expect(helpCard({ flagOn: () => false })).not.toContain("`/publish`");
    // …listed while it resolves on (same predicate the slash menu uses).
    expect(helpCard({ flagOn: (id) => id === "chat.publish" })).toContain("`/publish`");
    // Un-gated commands list regardless of the predicate.
    expect(helpCard({ flagOn: () => false })).toContain("`/compact`");
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

describe("/watch (ADR 0067)", () => {
  const cmd = () => findSlashCommand("watch")!;

  it("is registered and never falls through", () => {
    // Unlike /goal, there is NO server-side /watch to fall through to — returning false
    // would send the literal text "/watch" to the agent as a message. Every branch must
    // claim the token, including with no session and no form panel.
    expect(cmd()).toBeTruthy();
    expect(cmd().run(ctx())).toBe(true);
    expect(cmd().run(ctx({ rest: "new" }))).toBe(true);
    expect(cmd().run(ctx({ rest: "nonsense" }))).toBe(true);
  });

  it("/watch new opens the SAME form the Work panel's creator renders", async () => {
    let spec: ComposerFormSpec | null = null;
    // The command fetches the verifier catalog first, so `openForm` lands a microtask later —
    // `run` still returns true synchronously to intercept the send. With no server here the
    // fetch rejects and the form opens on the core types, which is the intended degradation.
    expect(cmd().run(ctx({ rest: "new", sessionId: "s1", openForm: (s) => (spec = s) }))).toBe(true);
    await vi.waitFor(() => expect(spec).not.toBeNull());
    const payload = spec!.payload;
    expect(payload.title).toBe("New watch");
    expect(payload.steps).toHaveLength(2);
    expect((payload.steps![0].schema as { required: string[] }).required).toEqual(["condition"]);
  });

  it("/watch new says so when the host has no form panel, instead of dropping the command", async () => {
    const notes: string[] = [];
    // openForm is optional in SlashContext — a host that never wired the panel must still
    // get a readable outcome.
    expect(cmd().run(ctx({ rest: "new", sessionId: "s1", noteToThread: (m) => notes.push(m) }))).toBe(true);
    await vi.waitFor(() => expect(notes.join(" ")).toMatch(/can't open the watch form/i));
  });

  it("an unknown subcommand explains the usage rather than guessing", () => {
    const notes: string[] = [];
    cmd().run(ctx({ rest: "clear all", noteToThread: (m) => notes.push(m) }));
    expect(notes.join(" ")).toMatch(/\/watch new/);
  });
});
