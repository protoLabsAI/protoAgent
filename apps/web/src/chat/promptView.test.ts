import { describe, expect, it } from "vitest";

import type { PromptCall } from "../lib/types";
import { budgetRows, callTabs, diffLine, fmtTok, promptNoteMarkdown, promptText, sectionDiff, sectionsLine, splitLine, usageLine } from "./promptView";

function mk(over: Partial<PromptCall> = {}): PromptCall {
  return {
    call_index: 0,
    ts: "2026-07-24T10:00:00+00:00",
    model: "claude-opus-4-7",
    system: { stable: "STABLE", context: "\n\n# Context\n\ntail" },
    usage: { input_tokens: 12345, output_tokens: 420, cache_read_tokens: 12000, cache_creation_tokens: 0 },
    ...over,
  };
}

describe("fmtTok", () => {
  it("uses the UsageFooter convention", () => {
    expect(fmtTok(999)).toBe("999");
    expect(fmtTok(12345)).toBe("12.3k");
    expect(fmtTok(1_200_000)).toBe("1.2M");
  });
});

describe("promptText", () => {
  it("concatenates stable + tail byte-for-byte", () => {
    expect(promptText(mk())).toBe("STABLE\n\n# Context\n\ntail");
  });
});

describe("callTabs", () => {
  it("maps calls to 1-based segmented tab items keyed by call_index", () => {
    const tabs = callTabs([mk(), mk({ call_index: 1 })]);
    expect(tabs).toEqual([
      { id: "0", label: "Call 1" },
      { id: "1", label: "Call 2" },
    ]);
  });
});

describe("usageLine", () => {
  it("renders in/out with the cache-read aside", () => {
    expect(usageLine(mk())).toBe("in 12.3k (cache read 12.0k) · out 420");
  });
  it("collapses when the call recorded no usage", () => {
    expect(
      usageLine(mk({ usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 } })),
    ).toBe("");
  });
});

describe("splitLine", () => {
  it("reports where the stable/tail split lands", () => {
    const call = mk({ system: { stable: "x".repeat(1500), context: "y".repeat(20) } });
    expect(splitLine(call)).toBe("stable 1.5k chars · context tail 20 chars");
  });
});

describe("budgetRows", () => {
  it("computes each section's share of the total prompt", () => {
    const call = mk({
      sections: [
        { label: "SOUL", chars: 750, approx_tokens: 187, scope: "stable" },
        { label: "Guidelines", chars: 200, approx_tokens: 50, scope: "stable" },
        { label: "Skills index", chars: 50, approx_tokens: 12, scope: "context" },
      ],
    });
    const rows = budgetRows(call);
    expect(rows.map((r) => r.pct)).toEqual([75, 20, 5]);
    expect(rows[2].scope).toBe("context");
  });
  it("keeps a sliver visible for tiny sections", () => {
    const call = mk({
      sections: [
        { label: "big", chars: 10_000, approx_tokens: 2500, scope: "stable" },
        { label: "tiny", chars: 3, approx_tokens: 0, scope: "context" },
      ],
    });
    expect(budgetRows(call)[1].pct).toBe(1);
  });
  it("is empty for unsegmented calls (pre-P2 rows or old servers)", () => {
    expect(budgetRows(mk())).toEqual([]);
    expect(budgetRows(mk({ sections: [] }))).toEqual([]);
  });
});

describe("sectionsLine", () => {
  it("joins per-section approx tokens for the /prompt note", () => {
    const call = mk({
      sections: [
        { label: "SOUL", chars: 40_800, approx_tokens: 10_200, scope: "stable" },
        { label: "Working state", chars: 76, approx_tokens: 19, scope: "context" },
      ],
    });
    expect(sectionsLine(call)).toBe("SOUL 10.2k · Working state 19");
    expect(sectionsLine(mk())).toBe("");
  });
});

describe("promptNoteMarkdown", () => {
  it("wraps the full text in a four-backtick fence when under the cap", () => {
    const md = promptNoteMarkdown(mk());
    expect(md).toContain("````text\nSTABLE\n\n# Context\n\ntail\n````");
    expect(md).toContain("`claude-opus-4-7`");
    expect(md).not.toContain("Showing");
  });
  it("adds the one-line budget when the call is segmented", () => {
    const md = promptNoteMarkdown(
      mk({ sections: [{ label: "SOUL", chars: 6, approx_tokens: 1, scope: "stable" }] }),
    );
    expect(md).toContain("_Budget (≈tokens):_ SOUL 1");
    expect(promptNoteMarkdown(mk())).not.toContain("Budget");
  });
  it("truncates at the cap and points to the full viewer", () => {
    const call = mk({ system: { stable: "s".repeat(50), context: "" } });
    const md = promptNoteMarkdown(call, 10);
    expect(md).toContain(`\`\`\`\`text\n${"s".repeat(10)}\n\`\`\`\``);
    expect(md).toContain("Showing 10 of 50 chars");
    expect(md).toContain("**View prompt**");
  });
  it("survives prompt bodies that contain triple-backtick fences", () => {
    const call = mk({ system: { stable: "docs:\n```py\nprint()\n```", context: "" } });
    const md = promptNoteMarkdown(call);
    // The outer 4-tick fence still closes AFTER the embedded 3-tick block.
    expect(md.indexOf("````text")).toBeLessThan(md.indexOf("```py"));
    expect(md.lastIndexOf("````")).toBeGreaterThan(md.indexOf("```py"));
  });
});

describe("sectionDiff (#2388 P3)", () => {
  const sec = (label: string, chars: number, scope: "stable" | "context" = "context") => ({
    label,
    chars,
    approx_tokens: Math.floor(chars / 4),
    scope,
  });

  it("matches sections on the base label so count-carrying labels resize instead of vanish+appear", () => {
    const prev = [sec("Injected memory (2 memories)", 100)];
    const cur = [sec("Injected memory (3 memories · 1 docs)", 160)];
    expect(sectionDiff(prev, cur)).toEqual([
      { label: "Injected memory (3 memories · 1 docs)", kind: "resized", delta: 60 },
    ]);
  });

  it("reports added and removed sections with signed char deltas", () => {
    const prev = [sec("Skills index", 50)];
    const cur = [sec("Working state", 30)];
    expect(sectionDiff(prev, cur)).toEqual([
      { label: "Working state", kind: "added", delta: 30 },
      { label: "Skills index", kind: "removed", delta: -50 },
    ]);
  });

  it("flags a same-size label change as relabeled", () => {
    const prev = [sec("Injected memory (2 memories)", 100)];
    const cur = [sec("Injected memory (1 sessions)", 100)];
    expect(sectionDiff(prev, cur)).toEqual([
      { label: "Injected memory (1 sessions)", kind: "relabeled", delta: 0 },
    ]);
  });

  it("returns [] for identical sections", () => {
    const rows = [sec("SOUL", 10, "stable"), sec("Skills index", 20)];
    expect(sectionDiff(rows, rows)).toEqual([]);
  });
});

describe("diffLine (#2388 P3)", () => {
  it("says so honestly when there is no comparison target", () => {
    expect(diffLine(null, "previous turn")).toBe("no comparison available");
  });

  it("says unchanged against the named anchor", () => {
    expect(diffLine([], "call 2")).toBe("unchanged vs call 2");
  });

  it("summarizes deltas and truncates past four", () => {
    const deltas = [
      { label: "A", kind: "added" as const, delta: 10 },
      { label: "B (2 memories)", kind: "removed" as const, delta: -20 },
      { label: "C (1 docs)", kind: "resized" as const, delta: 1200 },
      { label: "D", kind: "relabeled" as const, delta: 0 },
      { label: "E", kind: "added" as const, delta: 5 },
    ];
    const line = diffLine(deltas, "previous turn");
    expect(line).toContain("vs previous turn: + A");
    expect(line).toContain("− B");
    expect(line).toContain("C +1.2k chars");
    expect(line).toContain("1 more");
  });
});
