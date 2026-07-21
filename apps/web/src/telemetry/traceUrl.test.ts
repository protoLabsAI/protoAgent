import { describe, it, expect } from "vitest";

import { langfuseTraceUrl } from "./traceUrl";

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
