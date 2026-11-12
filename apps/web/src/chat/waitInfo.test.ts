// @vitest-environment node
import { describe, expect, it } from "vitest";

import { humanizeSeconds, parseWaitInput, resolveWaitInfo, summarizeThen } from "./waitInfo";

describe("parseWaitInput (#1914)", () => {
  it("parses the wait tool's {seconds, then} args", () => {
    expect(parseWaitInput('{"seconds": 300, "then": "Check the deploy."}')).toEqual({
      seconds: 300,
      then: "Check the deploy.",
    });
  });

  it("returns null for missing/empty/unparseable input — the fall-back-to-plain signal", () => {
    expect(parseWaitInput(undefined)).toBeNull();
    expect(parseWaitInput("")).toBeNull();
    // The 800-char server preview (server/chat.py::_TOOL_PREVIEW_CHARS) can cut a long
    // `then` mid-string — the JSON no longer parses.
    const truncated = `{"seconds": 300, "then": "${"x".repeat(900)}"}`.slice(0, 800);
    expect(parseWaitInput(truncated)).toBeNull();
    expect(parseWaitInput('{"seconds": 30')).toBeNull(); // mid-stream args
  });

  it("requires a finite numeric seconds; tolerates a missing then", () => {
    expect(parseWaitInput('{"then": "go"}')).toBeNull();
    expect(parseWaitInput('{"seconds": "300"}')).toBeNull();
    expect(parseWaitInput('{"seconds": 40}')).toEqual({ seconds: 40, then: "" });
  });

  it("clamps to >= 1 like the tool itself (max(1, int(seconds)))", () => {
    expect(parseWaitInput('{"seconds": 0}')).toEqual({ seconds: 1, then: "" });
    expect(parseWaitInput('{"seconds": -5}')).toEqual({ seconds: 1, then: "" });
  });
});

describe("resolveWaitInfo (#4852)", () => {
  it("recovers the full plan when the args preview is cut at 800 chars", () => {
    const then = "x".repeat(900);
    expect(resolveWaitInfo(
      JSON.stringify({ seconds: 600, then }).slice(0, 800),
      `Wait scheduled: 10 minutes. Will resume to: ${then}`,
    )).toEqual({ seconds: 600, then });
  });

  it("prefers valid JSON over output details", () => {
    expect(resolveWaitInfo('{"seconds": 30, "then": "input"}',
      "Wait scheduled: 1 minute. Will resume to: output",
    )).toEqual({ seconds: 30, then: "input" });
  });

  it.each([
    ["1 hour 30 minutes", 5400],
    ["1 second", 1],
    ["2 seconds", 2],
    ["1 minute", 60],
    ["2 hours", 7200],
    ["1 day 2 hours 3 minutes 4 seconds", 93784],
    ["2 days", 172800],
  ])("recovers %s from the output", (duration, seconds) => {
    expect(resolveWaitInfo("garbage", `Wait scheduled: ${duration}. Will resume to: x`))
      .toEqual({ seconds, then: "x" });
  });

  it.each(["Error: failed", "  eRrOr: failed", "ERROR scheduling"])(
    "rejects failed waits: %s", (output) => {
      expect(resolveWaitInfo('{"seconds": 600}', output)).toBeNull();
      expect(resolveWaitInfo('{"seconds": 600,', output)).toBeNull();
    },
  );

  it("returns null without a recoverable duration", () => {
    expect(resolveWaitInfo(undefined, undefined)).toBeNull();
    expect(resolveWaitInfo("garbage", "Wait scheduled: unknown. Will resume to: x")).toBeNull();
    expect(resolveWaitInfo("garbage", "Wait scheduled: 1 hour garbage.")).toBeNull();
  });

  it("recovers partial JSON escapes in a single pass", () => {
    expect(resolveWaitInfo(String.raw`{"seconds": 60, "then": "Check \"release\"\nC:\\new\\n`, undefined))
      .toEqual({ seconds: 60, then: 'Check "release"\nC:\\new\\n' });
  });

  it("preserves multiline output plans and prefers them over partial JSON", () => {
    expect(resolveWaitInfo('{"seconds": 30, "then": "partial',
      "Wait scheduled: 1 minute. Will resume to: first\nsecond",
    )).toEqual({ seconds: 30, then: "first\nsecond" });
  });

  it.each([[-5, 1], [0, 1], [1.5, 2]])("clamps and rounds recovered seconds %s", (raw, seconds) => {
    expect(resolveWaitInfo(`{"seconds": ${raw},`, undefined)).toEqual({ seconds, then: "" });
  });

  it("uses an empty plan when neither source supplies one", () => {
    expect(resolveWaitInfo(undefined, "Wait scheduled: 1 minute."))
      .toEqual({ seconds: 60, then: "" });
  });
});

describe("humanizeSeconds — loose mirror of tools/scheduler_tools.py::_humanize_duration", () => {
  it("phrases seconds / minutes / hours like the tool's own confirmation", () => {
    expect(humanizeSeconds(1)).toBe("1 second");
    expect(humanizeSeconds(40)).toBe("40 seconds");
    expect(humanizeSeconds(60)).toBe("1 minute");
    expect(humanizeSeconds(300)).toBe("5 minutes");
    expect(humanizeSeconds(90)).toBe("1 minute 30 seconds");
    expect(humanizeSeconds(3600)).toBe("1 hour");
    expect(humanizeSeconds(3900)).toBe("1 hour 5 minutes");
    expect(humanizeSeconds(7200)).toBe("2 hours");
  });
});

describe("summarizeThen — one readable line out of a long/technical resume plan", () => {
  it("keeps a short plan verbatim", () => {
    expect(summarizeThen("Check the build.")).toBe("Check the build.");
  });

  it("takes the first sentence of a multi-sentence plan", () => {
    expect(summarizeThen("Dock the ship. Then sell the ore. Then accept the next contract.")).toBe(
      "Dock the ship.",
    );
  });

  it("cuts an unbroken long plan at ~120 chars with an ellipsis", () => {
    const out = summarizeThen("do the thing with " + "very ".repeat(60) + "long args");
    expect(out.length).toBeLessThanOrEqual(120);
    expect(out.endsWith("…")).toBe(true);
  });

  it("collapses internal whitespace/newlines", () => {
    expect(summarizeThen("line one\n  line two.")).toBe("line one line two.");
  });

  it("does not split on a decimal point or an abbreviation-like dot mid-token", () => {
    expect(summarizeThen("Wait for v1.2 to deploy then verify")).toBe(
      "Wait for v1.2 to deploy then verify",
    );
  });
});
