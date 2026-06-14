import { describe, expect, it } from "vitest";

import {
  LARGE_PASTE_CHARS,
  LARGE_PASTE_LINES,
  filesFromTransfer,
  isLargePaste,
  namedFile,
  pastedTextFile,
} from "./paste";

describe("isLargePaste", () => {
  it("is false for empty / short text", () => {
    expect(isLargePaste("")).toBe(false);
    expect(isLargePaste("a quick message")).toBe(false);
  });

  it("is true over the character threshold", () => {
    expect(isLargePaste("x".repeat(LARGE_PASTE_CHARS + 1))).toBe(true);
    expect(isLargePaste("x".repeat(LARGE_PASTE_CHARS))).toBe(false);
  });

  it("is true over the line threshold even when short", () => {
    expect(isLargePaste("a\n".repeat(LARGE_PASTE_LINES + 1))).toBe(true);
    expect(isLargePaste("a\n".repeat(2))).toBe(false);
  });
});

describe("namedFile", () => {
  it("keeps an existing name", () => {
    const f = new File(["x"], "report.pdf", { type: "application/pdf" });
    expect(namedFile(f)).toBe(f);
  });

  it("names an unnamed image by its mime subtype", () => {
    const out = namedFile(new File(["x"], "", { type: "image/png" }));
    expect(out.name).toBe("pasted-image.png");
    expect(out.type).toBe("image/png");
  });

  it("falls back to a generic name for an unnamed typeless blob", () => {
    expect(namedFile(new File(["x"], "", { type: "" })).name).toBe("pasted-file.bin");
  });
});

describe("filesFromTransfer", () => {
  it("returns [] for a null payload", () => {
    expect(filesFromTransfer(null)).toEqual([]);
  });

  it("prefers items[].getAsFile() (clipboard images live there)", () => {
    const img = new File(["x"], "", { type: "image/png" });
    const dt = {
      items: [
        { kind: "string", getAsFile: () => null },
        { kind: "file", getAsFile: () => img },
      ],
      files: [],
    } as unknown as DataTransfer;
    const out = filesFromTransfer(dt);
    expect(out).toHaveLength(1);
    expect(out[0].name).toBe("pasted-image.png"); // unnamed → named
  });

  it("falls back to .files when items has no files", () => {
    const doc = new File(["x"], "notes.txt", { type: "text/plain" });
    const dt = { items: [], files: [doc] } as unknown as DataTransfer;
    expect(filesFromTransfer(dt)).toEqual([doc]);
  });
});

describe("pastedTextFile", () => {
  it("wraps text as a text/plain file", async () => {
    const f = pastedTextFile("hello world");
    expect(f.name).toBe("Pasted text.txt");
    expect(f.type).toBe("text/plain");
    expect(await f.text()).toBe("hello world");
  });
});
