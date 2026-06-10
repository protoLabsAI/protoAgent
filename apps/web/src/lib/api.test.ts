import { describe, it, expect } from "vitest";
import { drainSseBuffer, textFromParts, hitlFromParts } from "./api";

const HITL_MIME = "application/vnd.protolabs.hitl-v1+json";

function drain(buffer: string) {
  const frames: unknown[] = [];
  const rest = drainSseBuffer(buffer, (f) => frames.push(f));
  return { frames, rest };
}

describe("drainSseBuffer", () => {
  // The CRLF case is the regression that rendered blank chat bubbles: the a2a-sdk
  // emits `\r\n\r\n` event boundaries, and scanning only for `\n\n` matched zero.
  it("parses a frame on a CRLF boundary", () => {
    const { frames, rest } = drain(`data: {"v":1}\r\n\r\n`);
    expect(frames).toEqual([{ v: 1 }]);
    expect(rest).toBe("");
  });

  it("parses a frame on an LF boundary", () => {
    const { frames } = drain(`data: {"v":2}\n\n`);
    expect(frames).toEqual([{ v: 2 }]);
  });

  it("parses a frame on a CR boundary", () => {
    const { frames } = drain(`data: {"v":3}\r\r`);
    expect(frames).toEqual([{ v: 3 }]);
  });

  it("parses multiple frames from one buffer", () => {
    const { frames } = drain(`data: {"a":1}\r\n\r\ndata: {"b":2}\n\n`);
    expect(frames).toEqual([{ a: 1 }, { b: 2 }]);
  });

  it("leaves an incomplete trailing frame in the returned remainder", () => {
    const { frames, rest } = drain(`data: {"done":1}\n\ndata: {"partial":`);
    expect(frames).toEqual([{ done: 1 }]);
    expect(rest).toBe(`data: {"partial":`);
  });

  it("reassembles a boundary split across two chunks", () => {
    const first = drain(`data: {"split":1}\r`);
    expect(first.frames).toEqual([]); // boundary not yet complete
    const second = drain(first.rest + `\n\r\ndata: {"next":2}\n\n`);
    expect(second.frames).toEqual([{ split: 1 }, { next: 2 }]);
  });

  it("joins multi-line data: payloads and ignores non-data lines", () => {
    const { frames } = drain(`event: message\nid: 7\ndata: {"x":\ndata: 1}\n\n`);
    expect(frames).toEqual([{ x: 1 }]);
  });
});

describe("textFromParts", () => {
  it("concatenates text parts (treating undefined kind as text)", () => {
    expect(
      textFromParts([{ text: "he" }, { kind: "text", text: "llo" }]),
    ).toBe("hello");
  });

  it("skips non-text kinds and empty parts", () => {
    expect(
      textFromParts([{ kind: "data", text: "x" }, { kind: "text", text: "" }, { kind: "text", text: "ok" }]),
    ).toBe("ok");
  });

  it("returns an empty string for undefined parts", () => {
    expect(textFromParts(undefined)).toBe("");
  });
});

describe("hitlFromParts", () => {
  it("reads the A2A 1.0 member-discriminated form (content.$case=data)", () => {
    const parts = [
      { metadata: { mimeType: HITL_MIME }, content: { $case: "data", value: { question: "go?" } } },
    ];
    expect(hitlFromParts(parts)).toEqual({ question: "go?" });
  });

  it("reads the flattened proto-JSON form (top-level data)", () => {
    const parts = [{ metadata: { mimeType: HITL_MIME }, data: { question: "ok?" } }];
    expect(hitlFromParts(parts)).toEqual({ question: "ok?" });
  });

  it("returns null when no part matches the HITL mime", () => {
    expect(hitlFromParts([{ metadata: { mimeType: "text/plain" }, data: {} }])).toBeNull();
  });

  it("returns null for undefined parts", () => {
    expect(hitlFromParts(undefined)).toBeNull();
  });
});
