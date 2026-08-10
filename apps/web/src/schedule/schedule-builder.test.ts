import { describe, expect, it } from "vitest";

import { joinLocal } from "./dateParts";
import { buildOnce, buildRepeat, isPastOnce, parseSchedule } from "./schedule-builder";

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

  it("round-trips a one-off ISO stably (parse → rebuild yields the same UTC instant)", () => {
    const iso = buildOnce("2026-07-24T16:30"); // a local wall-clock time → UTC ISO
    const p = parseSchedule(iso);
    expect(p.mode).toBe("once");
    if (p.mode === "once") {
      expect(buildOnce(joinLocal(p.onceDate, p.onceTime))).toBe(iso);
    }
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
