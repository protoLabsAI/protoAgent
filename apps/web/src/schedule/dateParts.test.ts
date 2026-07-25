import { describe, expect, it } from "vitest";

import { from12h, joinLocal, monthGrid, nowTime, splitLocal, to12h, todayISO } from "./dateParts";

describe("date parts", () => {
  it("splits and rejoins a datetime-local string", () => {
    expect(splitLocal("2026-07-23T14:30")).toEqual({ date: "2026-07-23", time: "14:30" });
    expect(joinLocal("2026-07-23", "14:30")).toBe("2026-07-23T14:30");
  });
  it("defaults the time and refuses an empty date", () => {
    expect(joinLocal("2026-07-23", "")).toBe("2026-07-23T09:00");
    expect(joinLocal("", "14:30")).toBe("");
  });
  it("formats today + now in LOCAL time (never UTC)", () => {
    const d = new Date(2026, 6, 5, 8, 4); // Jul 5 2026, 08:04 local
    expect(todayISO(d)).toBe("2026-07-05");
    expect(nowTime(d)).toBe("08:04");
  });
});

describe("monthGrid", () => {
  it("is always a 6×7 rectangle, weeks starting Monday", () => {
    const g = monthGrid(2026, 6); // July 2026
    expect(g).toHaveLength(42);
    // July 1 2026 is a Wednesday → first cell is Mon Jun 29.
    expect(g[0]).toEqual({ iso: "2026-06-29", day: 29, inMonth: false });
    expect(g.find((c) => c.iso === "2026-07-01")).toEqual({ iso: "2026-07-01", day: 1, inMonth: true });
    expect(g.filter((c) => c.inMonth)).toHaveLength(31);
  });
});

describe("12/24h conversion round-trips", () => {
  it("converts both ways", () => {
    expect(to12h("00:00")).toEqual({ h12: 12, minute: "00", ampm: "AM" });
    expect(to12h("13:45")).toEqual({ h12: 1, minute: "45", ampm: "PM" });
    expect(to12h("12:30")).toEqual({ h12: 12, minute: "30", ampm: "PM" });
    expect(from12h(12, "00", "AM")).toBe("00:00");
    expect(from12h(1, "45", "PM")).toBe("13:45");
    expect(from12h(12, "30", "PM")).toBe("12:30");
  });
});
