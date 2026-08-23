import { describe, it, expect } from "vitest";

import { langfuseTraceUrl, traceCellState } from "./traceUrl";

const TPL = "https://langfuse.example.com/project/cmp123/traces/{trace_id}";

describe("langfuseTraceUrl", () => {
  it("fills the {trace_id} placeholder from the server template", () => {
    expect(langfuseTraceUrl(TPL, "abc123")).toBe(
      "https://langfuse.example.com/project/cmp123/traces/abc123",
    );
  });

  it("returns null with no template — the surface falls back to a copyable id", () => {
    expect(langfuseTraceUrl(null, "abc123")).toBeNull();
    expect(langfuseTraceUrl(undefined, "abc123")).toBeNull();
    expect(langfuseTraceUrl("", "abc123")).toBeNull();
  });

  it("returns null when the row has no trace id (Langfuse was off for that turn)", () => {
    expect(langfuseTraceUrl(TPL, null)).toBeNull();
    expect(langfuseTraceUrl(TPL, "   ")).toBeNull();
  });

  it("refuses a template missing the placeholder rather than linking to the wrong trace", () => {
    expect(langfuseTraceUrl("https://langfuse.example.com/project/cmp123/traces/", "abc")).toBeNull();
  });

  it("refuses a non-http(s) template (no javascript: hrefs)", () => {
    expect(langfuseTraceUrl("javascript:alert('{trace_id}')", "abc")).toBeNull();
  });

  it("url-encodes the trace id", () => {
    expect(langfuseTraceUrl(TPL, "a b/c")).toBe(
      "https://langfuse.example.com/project/cmp123/traces/a%20b%2Fc",
    );
  });
});

// #3017 — an always-empty Trace column reads as "these turns weren't traced", not
// "tracing is disabled". The cell has to tell those apart.
describe("traceCellState", () => {
  it("deep-links a traced row when the server sent a template", () => {
    expect(traceCellState(TPL, "abc123def456", true)).toEqual({
      kind: "link",
      href: "https://langfuse.example.com/project/cmp123/traces/abc123def456",
      short: "abc123de",
    });
  });

  it("falls back to a copyable id when the template is unavailable", () => {
    expect(traceCellState(null, "abc123def456", true)).toEqual({
      kind: "copy",
      traceId: "abc123def456",
      short: "abc123de",
    });
  });

  it("says 'off' for an untraced row when tracing is disabled", () => {
    expect(traceCellState(TPL, null, false)).toEqual({ kind: "off" });
    expect(traceCellState(TPL, "  ", false)).toEqual({ kind: "off" });
  });

  it("says 'none' for an untraced row while tracing is ON — that turn simply has no trace", () => {
    expect(traceCellState(TPL, null, true)).toEqual({ kind: "none" });
  });

  it("still shows a stored trace id after tracing is turned off — the trace still exists", () => {
    expect(traceCellState(TPL, "abc123def456", false)).toEqual({
      kind: "link",
      href: "https://langfuse.example.com/project/cmp123/traces/abc123def456",
      short: "abc123de",
    });
  });
});
