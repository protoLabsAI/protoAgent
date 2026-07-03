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
});
