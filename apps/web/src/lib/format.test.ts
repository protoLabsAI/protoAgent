import { describe, expect, it } from "vitest";

import { bytes, localStamp, localStampFull, localStampTitle } from "./format";

// #2468 — telemetry stamps must render as local wall-clock instants, not a
// sliced copy of the source UTC string. Assertions compare against the same
// Date the formatter should produce, so they hold in any CI timezone.
const p = (n: number) => String(n).padStart(2, "0");
const expectedStamp = (d: Date) =>
  `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;

describe("localStamp", () => {
  it("renders an offset-carrying ISO instant in the local timezone", () => {
    const iso = "2026-08-10T15:45:45.123456+00:00";
    expect(localStamp(iso)).toBe(expectedStamp(new Date(Date.parse(iso))));
  });

  it("treats an offsetless value as UTC (legacy rows), never as local", () => {
    expect(localStamp("2026-08-10T15:45:45")).toBe(localStamp("2026-08-10T15:45:45Z"));
  });

  it("degrades to a dash on garbage/empty", () => {
    expect(localStamp("")).toBe("—");
    expect(localStamp(undefined)).toBe("—");
    expect(localStamp("not-a-date")).toBe("—");
  });
});

describe("localStampFull", () => {
  it("names the timezone so the tooltip carries the offset context", () => {
    // timeZoneName:"short" appends a zone label ("EDT", "GMT+2", "UTC") —
    // assert one exists rather than pinning the CI zone.
    const full = localStampFull("2026-08-10T15:45:45+00:00");
    expect(full).toMatch(/\d{2}:\d{2}:\d{2}/);
    expect(full).toMatch(/[A-Za-z]/);
  });

  it("falls back to the raw input when unparseable", () => {
    expect(localStampFull("not-a-date")).toBe("not-a-date");
  });
});

describe("localStampTitle", () => {
  it("keeps the raw ISO (microseconds + original offset) next to the local rendering", () => {
    const iso = "2026-08-10T15:45:45.123456+00:00";
    const title = localStampTitle(iso);
    expect(title).toContain(iso);
    expect(title).toContain(localStampFull(iso));
  });

  it("degrades to the raw input alone when unparseable, and a dash when empty", () => {
    expect(localStampTitle("not-a-date")).toBe("not-a-date");
    expect(localStampTitle("")).toBe("—");
  });
});

describe("bytes", () => {
  it("renders sub-KB counts raw", () => {
    expect(bytes(0)).toBe("0 B");
    expect(bytes(512)).toBe("512 B");
    expect(bytes(1023)).toBe("1023 B");
  });

  it("renders KB with one decimal", () => {
    expect(bytes(1024)).toBe("1.0 KB");
    expect(bytes(2048)).toBe("2.0 KB");
    expect(bytes(2150)).toBe("2.1 KB");
  });

  it("renders MB with one decimal", () => {
    expect(bytes(1_048_576)).toBe("1.0 MB");
    expect(bytes(3_565_158)).toBe("3.4 MB");
  });

  it("drops the decimal at 10+ in a unit (the store-size convention)", () => {
    expect(bytes(15_360)).toBe("15 KB");
    expect(bytes(104_857_600)).toBe("100 MB");
  });

  it("renders GB and saturates there", () => {
    expect(bytes(2_147_483_648)).toBe("2.0 GB");
    expect(bytes(1_099_511_627_776)).toBe("1024 GB");
  });
});
