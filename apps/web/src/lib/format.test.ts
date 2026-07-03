import { describe, expect, it } from "vitest";

import { bytes } from "./format";

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
