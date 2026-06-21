import { describe, it, expect } from "vitest";
import { ApiError, apiUrl, drainSseBuffer, isColdStart, textFromParts, hitlFromParts } from "./api";

describe("cold-start detection (ApiError / isColdStart)", () => {
  it("ApiError carries the HTTP status", () => {
    const e = new ApiError(409, "agent 'x' is not running");
    expect(e.status).toBe(409);
    expect(e).toBeInstanceOf(Error);
  });

  it("treats 409 (member spawning) and 502 (booting) as cold-start", () => {
    expect(isColdStart(new ApiError(409, "not running"))).toBe(true);
    expect(isColdStart(new ApiError(502, "not reachable"))).toBe(true);
  });

  it("does NOT treat real HTTP failures (404/500) as cold-start", () => {
    expect(isColdStart(new ApiError(404, "nope"))).toBe(false);
    expect(isColdStart(new ApiError(500, "boom"))).toBe(false);
  });

  it("treats a fetch with no HTTP response as cold-start (desktop sidecar booting)", () => {
    // WKWebView throws `TypeError: Load failed` (Chrome: "Failed to fetch") when the
    // local sidecar isn't bound to its port yet on first launch — ride it out rather
    // than flashing "Load failed" in the tasks/notes panels.
    expect(isColdStart(new TypeError("Load failed"))).toBe(true);
    expect(isColdStart(new Error("Failed to fetch"))).toBe(true);
  });
});

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

describe("apiUrl — fleet slug routing (ADR 0042)", () => {
  // currentSlug() reads /app/agent/<slug>/ from the URL; drive it via history.
  const focus = (slug: string | null) =>
    window.history.replaceState({}, "", slug ? `/app/agent/${slug}/` : "/app/");

  it("does not slug-prefix on the host window", () => {
    focus(null);
    expect(apiUrl("/plugins/agent_browser/panel")).not.toContain("/agents/");
    expect(apiUrl("/api/runtime/status")).not.toContain("/agents/");
  });

  it("routes a DEFAULT-prefix plugin view (/plugins/<id>/…) to the focused member", () => {
    // The 404 regression: agent_browser/project_board views use the registry's default
    // /plugins/ prefix; without that in isAgentPath, a member's view iframe hit the hub.
    focus("protoPlugins-abf8");
    expect(apiUrl("/plugins/agent_browser/panel")).toContain(
      "/agents/protoPlugins-abf8/plugins/agent_browser/panel",
    );
  });

  it("routes custom-prefix plugin views (/api/plugins/<id>/…) and the agent API", () => {
    focus("m");
    expect(apiUrl("/api/plugins/notes/note")).toContain("/agents/m/api/plugins/notes/note");
    expect(apiUrl("/api/runtime/status")).toContain("/agents/m/api/runtime/status");
  });

  it("keeps hub control-plane paths on the hub even in a member window", () => {
    focus("m");
    expect(apiUrl("/api/fleet")).not.toContain("/agents/");
    expect(apiUrl("/api/archetypes")).not.toContain("/agents/");
  });
});
