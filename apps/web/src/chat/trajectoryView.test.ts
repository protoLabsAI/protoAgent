import { describe, expect, it } from "vitest";

import { eventLine, trajectoryNoteMarkdown } from "./trajectoryView";
import type { TrajectoryCall, TrajectoryEvent } from "../lib/types";

const req: TrajectoryEvent = {
  index: 0, t: "request", model: "claude-opus-4-8", tools_count: 12,
  msgs: [{ id: "h1", role: "human", sha: "a", chars: 4000 }, { id: "a1", role: "ai", sha: "b", chars: 8000 }],
};

describe("eventLine", () => {
  it("renders a request as a call row with token-ish size", () => {
    expect(eventLine(req)).toBe("→ call · claude-opus-4-8 · 2 msgs (~3.0k tok) · 12 tools");
  });

  it("renders a response with the cache share", () => {
    expect(eventLine({ index: 1, t: "response", status: "ok", usage: { input: 1000, output: 50, cache_read: 900 } }))
      .toBe("← ok · 1,000 in / 50 out · 90% cached");
  });

  it("renders a surface op as a flagged rewrite", () => {
    expect(eventLine({ index: 2, t: "surface_op", op: "prune", cause: "pressure>=60%", rewritten_ids: ["m1", "m2"] }))
      .toBe("⚠ prune (pressure>=60%) — 2 rewritten");
  });

  it("ignores unknown event kinds rather than throwing", () => {
    expect(eventLine({ index: 3, t: "future_thing" })).toBeNull();
  });
});

describe("trajectoryNoteMarkdown", () => {
  it("is honest when nothing is recorded yet", () => {
    expect(trajectoryNoteMarkdown([], 0, null)).toContain("Nothing recorded");
  });

  it("summarizes the tail and the latest call's availability", () => {
    const call: TrajectoryCall = {
      found: true, call: 4, calls: 5, model: "m", ts: "",
      messages: [
        { id: "h1", role: "human", sha: "a", chars: 10, status: "available" },
        { id: "x", role: "tool", sha: "b", chars: 9000, status: "missing" },
      ],
      availability: { available: 1, rewritten: 0, missing: 1 },
    };
    const note = trajectoryNoteMarkdown([req], 40, call);
    expect(note).toContain("last 1 of 40 events");
    expect(note).toContain("Latest call (#5/5)");
    expect(note).toContain("1 available · 0 rewritten in place · 1 gone");
    expect(note).toContain("the chat archive may hold the text");
  });
});
