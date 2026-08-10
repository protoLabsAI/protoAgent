import { describe, expect, it } from "vitest";

import { joinLocal } from "./dateParts";
import { buildOnce, buildRepeat, canonicalSchedule, cronFieldError, isPastOnce, parseSchedule } from "./schedule-builder";

describe("parseSchedule — detect which builder mode a stored schedule came from (#2159)", () => {
  it("recovers the friendly recurring presets", () => {
    expect(parseSchedule(buildRepeat("daily", "09:00", 1))).toEqual({ mode: "repeat", freq: "daily", time: "09:00", dow: 1 });
    expect(parseSchedule(buildRepeat("weekdays", "07:30", 1))).toEqual({ mode: "repeat", freq: "weekdays", time: "07:30", dow: 1 });
    expect(parseSchedule(buildRepeat("weekly", "18:15", 3))).toEqual({ mode: "repeat", freq: "weekly", time: "18:15", dow: 3 });
    const hourly = parseSchedule(buildRepeat("hourly", "00:20", 1));
    expect(hourly.mode).toBe("repeat");
    if (hourly.mode === "repeat") expect(hourly.freq).toBe("hourly");
  });

  it("falls back to raw cron when it doesn't match a preset (never lossy)", () => {
    expect(parseSchedule("*/5 * * * *")).toEqual({ mode: "cron", cronRaw: "*/5 * * * *" });
    expect(parseSchedule("0 9 1 * *")).toEqual({ mode: "cron", cronRaw: "0 9 1 * *" }); // day-of-month
    expect(parseSchedule("not a schedule")).toEqual({ mode: "cron", cronRaw: "not a schedule" });
  });

  it("keeps an out-of-range 'preset-shaped' cron raw (never silently clamps it)", () => {
    expect(parseSchedule("60 9 * * *")).toEqual({ mode: "cron", cronRaw: "60 9 * * *" });
    expect(parseSchedule("0 24 * * *")).toEqual({ mode: "cron", cronRaw: "0 24 * * *" });
  });

  it("round-trips a one-off ISO stably (parse → rebuild yields the same UTC instant)", () => {
    const iso = buildOnce("2026-07-24T16:30"); // a local wall-clock time → UTC ISO
    const p = parseSchedule(iso);
    expect(p.mode).toBe("once");
    if (p.mode === "once") {
      expect(buildOnce(joinLocal(p.onceDate, p.onceTime))).toBe(iso);
    }
  });
});

describe("cronFieldError — range validation beyond the field count (#2439 review)", () => {
  it("accepts real-world expressions", () => {
    for (const s of ["0 9 * * *", "*/5 * * * *", "0 9 * * 1-5", "15,45 8-18 * * *", "0 0 1 1 *", "0 9 * * 7"]) {
      expect(cronFieldError(s)).toBe("");
    }
  });

  it("rejects out-of-range field values", () => {
    expect(cronFieldError("60 9 * * *")).toMatch(/minute/);
    expect(cronFieldError("0 24 * * *")).toMatch(/hour/);
    expect(cronFieldError("0 9 32 * *")).toMatch(/day-of-month/);
    expect(cronFieldError("0 9 * 13 *")).toMatch(/month/);
    expect(cronFieldError("0 9 * * 8")).toMatch(/day-of-week/);
    expect(cronFieldError("9-5 * * * *")).toMatch(/minute/); // inverted range
    expect(cronFieldError("*/0 * * * *")).toMatch(/never fires/);
  });

  it("still enforces the field count and rejects junk atoms", () => {
    expect(cronFieldError("0 9 * *")).toMatch(/exactly 5 fields/);
    expect(cronFieldError("a b c d e")).toMatch(/Unrecognized/);
  });
});

describe("canonicalSchedule — the builder's normalized form (#2439 review)", () => {
  it("absorbs cosmetic cron differences a preset re-format would make", () => {
    expect(canonicalSchedule("0 09 * * *")).toBe("0 9 * * *"); // leading-zero hour
    expect(canonicalSchedule("05 * * * *")).toBe("5 * * * *"); // leading-zero minute
  });

  it("passes custom cron and unparseable strings through untouched", () => {
    expect(canonicalSchedule("*/5 * * * *")).toBe("*/5 * * * *");
    expect(canonicalSchedule("0 9 1 * *")).toBe("0 9 1 * *");
    expect(canonicalSchedule("not a schedule")).toBe("not a schedule");
  });

  it("is a fixed point for anything the builder itself emitted", () => {
    for (const s of [buildRepeat("daily", "09:00", 1), buildRepeat("hourly", "00:20", 1), buildRepeat("weekly", "18:15", 3)]) {
      expect(canonicalSchedule(s)).toBe(s);
    }
    const iso = buildOnce("2026-07-24T16:30");
    expect(canonicalSchedule(iso)).toBe(iso);
  });
});

describe("isPastOnce — the one-off that silently never fires (#2159)", () => {
  const now = new Date("2026-07-24T12:00:00Z");
  it("flags a past one-off", () => {
    expect(isPastOnce("2026-07-24T11:00:00Z", now)).toBe(true);
  });
  it("passes a future one-off", () => {
    expect(isPastOnce("2026-07-24T13:00:00Z", now)).toBe(false);
  });
  it("never flags a recurring cron (it's not a one-off)", () => {
    expect(isPastOnce("0 9 * * *", now)).toBe(false);
    expect(isPastOnce("", now)).toBe(false);
  });
});
