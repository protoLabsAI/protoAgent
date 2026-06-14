import { describe, expect, it } from "vitest";

import { byRecency, fmtElapsed } from "./background-jobs";
import type { BackgroundJobDTO } from "../lib/types";

function job(p: Partial<BackgroundJobDTO>): BackgroundJobDTO {
  return { id: "x", status: "completed", subagent_type: "researcher", description: "d", ...p };
}

describe("fmtElapsed", () => {
  it("formats sub-minute / minute / hour spans", () => {
    const t0 = "2026-06-14T00:00:00.000Z";
    expect(fmtElapsed(t0, "2026-06-14T00:00:12.000Z")).toBe("12s");
    expect(fmtElapsed(t0, "2026-06-14T00:03:04.000Z")).toBe("3m 4s");
    expect(fmtElapsed(t0, "2026-06-14T02:05:00.000Z")).toBe("2h 5m");
  });

  it("returns empty for missing/invalid start", () => {
    expect(fmtElapsed(undefined)).toBe("");
    expect(fmtElapsed("not-a-date", "2026-06-14T00:00:01.000Z")).toBe("");
  });
});

describe("byRecency", () => {
  it("sorts running jobs ahead of finished ones", () => {
    const running = job({ id: "r", status: "running", created_at: "2026-06-14T00:00:00Z" });
    const done = job({ id: "d", status: "completed", completed_at: "2026-06-14T01:00:00Z" });
    expect([done, running].sort(byRecency).map((j) => j.id)).toEqual(["r", "d"]);
  });

  it("orders finished jobs newest-first", () => {
    const older = job({ id: "o", completed_at: "2026-06-14T00:00:00Z" });
    const newer = job({ id: "n", completed_at: "2026-06-14T02:00:00Z" });
    expect([older, newer].sort(byRecency).map((j) => j.id)).toEqual(["n", "o"]);
  });
});
