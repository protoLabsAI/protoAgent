import { describe, expect, it } from "vitest";
import type { Command } from "@protolabsai/ui/command-palette";

import { TIER, isSubsequence, matchCommand, orderCommands, rankCommands, tierFor } from "./rank";

// Two contracts live here and they pull in opposite directions, which is exactly why they
// are tested apart:
//
//   1. INCLUSION is a verbatim copy of the DS's module-private `matchCommand`
//      (@protolabsai/ui/src/command-palette.views.tsx:48-56). The parity block below pins
//      the four properties that define it — but note what that block CANNOT do: it describes
//      our copy, so it would pass unchanged if the DS rewrote its matcher tomorrow. The
//      alarm for THAT is in `rootView.test.ts` ("matchCommand parity — the DRIFT ALARM"),
//      which drives the DS's real commands view and compares the rows it renders against
//      this function's verdict. Both are needed; neither substitutes for the other.
//   2. ORDER is ours. It may reorder anything; it may never DROP anything, and it may never
//      cap. `fleet.spec.ts:421-436` is the live proof: it types "ava" and expects the Fleet
//      Room row, which matches only because every member name is pushed onto that command's
//      keywords. A label-first ranking that discarded keyword-only hits would red that spec
//      in e2e, minutes later, far from the cause.

const cmd = (c: Partial<Command> & { id: string; label: string }): Command => ({
  run: () => {},
  ...c,
});

describe("matchCommand — DS parity (a copy, so pin it)", () => {
  it("admits everything on an empty or whitespace query", () => {
    const c = cmd({ id: "a", label: "Anything" });
    expect(matchCommand(c, "")).toBe(true);
    expect(matchCommand(c, "   ")).toBe(true);
  });

  it("requires EVERY whitespace-separated term (AND, not OR)", () => {
    const c = cmd({ id: "a", label: "Settings", keywords: ["config"] });
    expect(matchCommand(c, "settings config")).toBe(true);
    expect(matchCommand(c, "settings nope")).toBe(false);
  });

  it("searches label, hint, group, source.label and keywords, case-insensitively", () => {
    const c = cmd({
      id: "a",
      label: "Zed",
      hint: "GO TO",
      group: "Plugins",
      source: { id: "p", label: "boardy" },
      keywords: ["Kanban"],
    });
    for (const q of ["zed", "go to", "plugins", "BOARDY", "kanban"]) {
      expect(matchCommand(c, q), q).toBe(true);
    }
  });

  it("is a substring test, not a prefix or word test", () => {
    expect(matchCommand(cmd({ id: "a", label: "Knowledge" }), "nowled")).toBe(true);
  });
});

describe("tiers", () => {
  const c = (label: string, extra: Partial<Command> = {}) => cmd({ id: label, label, ...extra });

  it("orders exact > prefix > word-prefix > substring > metadata", () => {
    expect(tierFor(c("Memory"), "memory")).toBe(TIER.EXACT);
    expect(tierFor(c("Memory inspector"), "memory")).toBe(TIER.PREFIX);
    expect(tierFor(c("Hot Memory"), "memo")).toBe(TIER.WORD_PREFIX);
    expect(tierFor(c("Remembered"), "ememb")).toBe(TIER.SUBSTRING);
    expect(tierFor(c("Fleet Room", { keywords: ["ava"] }), "ava")).toBe(TIER.META);
  });

  it("splits ids and punctuation into words, so 'fleet' word-prefixes 'Settings: Fleet'", () => {
    expect(tierFor(c("Settings: Fleet"), "fleet")).toBe(TIER.WORD_PREFIX);
    expect(tierFor(c("plugin:notes:editor"), "notes")).toBe(TIER.WORD_PREFIX);
  });

  it("falls back to a label subsequence, then to a split label/metadata match", () => {
    expect(tierFor(c("Knowledge"), "kwg")).toBe(TIER.FUZZY);
    // The residual bucket: "bra" only matches inside the label, "goals" only the keyword,
    // so no single-field tier describes it — yet `matchCommand` (one joined haystack)
    // admits it, which is precisely why the bucket has to exist.
    expect(tierFor(c("Zebra", { keywords: ["goals"] }), "bra goals")).toBe(TIER.SPLIT);
    // A term that word-prefixes the label still wins that stronger tier, even when the
    // other term lives in the metadata — label affinity is the signal being graded.
    expect(tierFor(c("Work", { keywords: ["goals"] }), "work goal")).toBe(TIER.WORD_PREFIX);
  });
});

describe("isSubsequence", () => {
  it("matches characters in order, not contiguously", () => {
    expect(isSubsequence("kwg", "knowledge")).toBe(true);
    expect(isSubsequence("gwk", "knowledge")).toBe(false);
    expect(isSubsequence("", "anything")).toBe(true);
  });
});

describe("rankCommands", () => {
  const corpus: Command[] = [
    cmd({ id: "fleet-room", label: "Fleet Room", keywords: ["ava", "roxy", "broadcast"] }),
    cmd({ id: "open:memory", label: "Memory", keywords: ["open", "go", "surface"] }),
    cmd({ id: "mem:hot", label: "Hot memory digest" }),
    cmd({ id: "settings", label: "Settings", keywords: ["memory", "config"] }),
  ];

  it("puts an exact label match above a prefix, and a prefix above a keyword hit", () => {
    const ids = rankCommands(corpus, "memory").map((c) => c.id);
    expect(ids[0]).toBe("open:memory"); // exact
    expect(ids.indexOf("mem:hot")).toBeLessThan(ids.indexOf("settings")); // label > keyword
  });

  it("keeps a keyword-only match — 'ava' still finds the Fleet Room (fleet.spec.ts:431)", () => {
    expect(rankCommands(corpus, "ava").map((c) => c.id)).toContain("fleet-room");
  });

  it("never shrinks the set matchCommand admits, and never caps", () => {
    const matching = corpus.filter((c) => matchCommand(c, "memory"));
    expect(rankCommands(corpus, "memory")).toHaveLength(matching.length);
    // Every row matches "e"; all of them come back.
    expect(rankCommands(corpus, "e")).toHaveLength(corpus.length);
  });

  it("hands the empty query back untouched — that list is the view's, not the ranker's", () => {
    expect(rankCommands(corpus, "  ").map((c) => c.id)).toEqual(corpus.map((c) => c.id));
  });

  it("breaks ties on frecency, but only WITHIN a tier", () => {
    const tied: Command[] = [
      cmd({ id: "a", label: "Publish" }),
      cmd({ id: "b", label: "Publish" }),
    ];
    // Same tier: the frecent one wins despite being registered second.
    expect(rankCommands(tied, "publish", { score: (id) => (id === "b" ? 99 : 0) })[0].id).toBe("b");
    // Across tiers it must NOT: a heavily-used keyword match still sorts under a fresh
    // prefix match, so what you are typing always beats what you once ran.
    const mixed: Command[] = [
      cmd({ id: "kw", label: "Zebra", keywords: ["publish"] }),
      cmd({ id: "pfx", label: "Publish notes" }),
    ];
    expect(rankCommands(mixed, "publish", { score: (id) => (id === "kw" ? 1e6 : 0) })[0].id).toBe("pfx");
  });

  it("is stable: equal tier and equal frecency keep registration order", () => {
    const same: Command[] = ["one", "two", "three"].map((id) => cmd({ id, label: "Same label" }));
    expect(rankCommands(same, "same").map((c) => c.id)).toEqual(["one", "two", "three"]);
  });

  it("does not mutate the input array", () => {
    const input = [...corpus];
    rankCommands(input, "memory");
    expect(input.map((c) => c.id)).toEqual(corpus.map((c) => c.id));
  });
});

describe("orderCommands — sort without filter (the provider path)", () => {
  const remote: Command[] = [
    cmd({ id: "card:7", label: "Sprint board" }),
    cmd({ id: "card:2", label: "Kanban cleanup" }),
  ];

  it("keeps a row that does NOT match the query at all", () => {
    // A provider is a REMOTE search that already applied the query its own way, so the DS
    // appends its rows verbatim and a row sharing no substring with the query is legitimate.
    // `rankCommands` would drop "Sprint board" here; the split exists so it can't.
    expect(matchCommand(remote[0], "kanban")).toBe(false);
    expect(orderCommands(remote, "kanban").map((c) => c.id)).toHaveLength(2);
  });

  it("still ORDERS what it keeps — the matching row leads", () => {
    expect(orderCommands(remote, "kanban")[0].id).toBe("card:2");
  });

  it("is what rankCommands is built from, so the two can never grade differently", () => {
    const corpus = [...remote, cmd({ id: "x", label: "Kanban board" })];
    const matching = corpus.filter((c) => matchCommand(c, "kanban"));
    expect(rankCommands(corpus, "kanban")).toEqual(orderCommands(matching, "kanban"));
  });
});
