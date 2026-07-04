import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import {
  ApiError,
  api,
  apiUrl,
  artifactAppends,
  drainSseBuffer,
  frameIsForeign,
  isColdStart,
  isAgentNotRunning,
  isAgentUnreachable,
  isMemberScoped,
  loadBackgroundReport,
  textFromParts,
  hitlFromParts,
} from "./api";
import { authRequired, clearAuthRequired } from "./auth";

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

describe("focused-agent-down detection (isAgentNotRunning)", () => {
  it("true ONLY for a 409 (the fleet proxy's 'agent not running')", () => {
    expect(isAgentNotRunning(new ApiError(409, "agent 'x' is not running"))).toBe(true);
    // 502 is a proxy/boot hiccup, not a definitively-down agent — stays a cold-start retry, not recovery.
    expect(isAgentNotRunning(new ApiError(502, "not reachable"))).toBe(false);
    expect(isAgentNotRunning(new ApiError(404, "nope"))).toBe(false);
    expect(isAgentNotRunning(new TypeError("Load failed"))).toBe(false);
    expect(isAgentNotRunning(undefined)).toBe(false);
  });
});

describe("unreachable-remote detection (isAgentUnreachable)", () => {
  it("true ONLY for a 502 (the fleet proxy's 'can't reach the member') — a remote never 409s", () => {
    expect(isAgentUnreachable(new ApiError(502, "agent is not reachable"))).toBe(true);
    expect(isAgentUnreachable(new ApiError(409, "not running"))).toBe(false); // that's a local peer
    expect(isAgentUnreachable(new ApiError(401, "unauthorized"))).toBe(false); // that's a bad token
    expect(isAgentUnreachable(new TypeError("Load failed"))).toBe(false);
    expect(isAgentUnreachable(undefined)).toBe(false);
  });
});

describe("isMemberScoped — which 401s belong to a member vs the hub (ADR 0042 §I)", () => {
  const focus = (slug: string | null) =>
    window.history.replaceState({}, "", slug ? `/app/agent/${slug}/` : "/app/");

  it("host window: nothing is member-scoped", () => {
    focus(null);
    expect(isMemberScoped("/api/runtime/status")).toBe(false);
    expect(isMemberScoped("/a2a")).toBe(false);
  });

  it("member window: slug-routed agent paths are member-scoped, hub paths are not", () => {
    focus("ava");
    expect(isMemberScoped("/api/runtime/status")).toBe(true); // proxied to the member
    expect(isMemberScoped("/a2a")).toBe(true);
    expect(isMemberScoped("/api/fleet")).toBe(false); // hub control-plane — stays on the hub
    expect(isMemberScoped("/api/runtime/status", true)).toBe(false); // host:true forces the hub
  });
});

describe("member-scoped 401 must NOT hijack the hub AuthGate (ADR 0042 §I)", () => {
  // The load-bearing behavior: a proxied member's bad/rotated token 401s, but that's the
  // MEMBER's credential problem — tripping the global hub prompt would ask for (and overwrite)
  // the wrong token. request() suppresses notifyAuthRequired() for member-scoped requests.
  const focus = (slug: string | null) =>
    window.history.replaceState({}, "", slug ? `/app/agent/${slug}/` : "/app/");
  const unauthorized = () =>
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 401,
        statusText: "Unauthorized",
        text: async () => "unauthorized",
      })),
    );

  beforeEach(() => clearAuthRequired());
  afterEach(() => {
    vi.unstubAllGlobals();
    clearAuthRequired();
  });

  it("a HOST-scoped 401 trips the global auth prompt", async () => {
    focus(null);
    unauthorized();
    await expect(api.runtimeStatus()).rejects.toBeInstanceOf(ApiError);
    expect(authRequired()).toBe(true);
  });

  it("a MEMBER-scoped 401 does NOT trip it (it's the member's token, not the hub's)", async () => {
    focus("ava");
    unauthorized();
    await expect(api.runtimeStatus()).rejects.toBeInstanceOf(ApiError);
    expect(authRequired()).toBe(false); // the boot gate / fleet panel own this recovery instead
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

describe("artifactAppends — the A2A append flag's wire shape (#1709 companion)", () => {
  // proto3 gives `append` no presence: the SDK OMITS the key at false, so the
  // terminal frame that REPLACES the artifact with the full canonical answer
  // arrives with NO `append` at all. Reading absent as append (`append !== false`,
  // the old mapping) re-appended the whole answer — every streamed turn doubled.
  it("an ABSENT append key is a REPLACE (the terminal last-chunk frame's wire shape)", () => {
    expect(artifactAppends({ artifact: { parts: [{ text: "full answer" }] }, lastChunk: true })).toBe(false);
  });

  it("an explicit append:true is an append (mid-stream chunk frames)", () => {
    expect(artifactAppends({ append: true })).toBe(true);
  });

  it("an explicit append:false is a replace", () => {
    expect(artifactAppends({ append: false })).toBe(false);
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

  it("host:true keeps the SSE token on the hub in a member window (proxied /api/events is validated by the HUB key)", () => {
    // The SSE-token fix: /api/events is proxied and the HUB's auth middleware validates the
    // ?token= with the HUB's key before forwarding, so the token must be hub-signed. Without
    // host:true, /api/sse-token would slug-route and be member-signed → 401 at the hub.
    focus("m");
    expect(apiUrl("/api/sse-token")).toContain("/agents/m/api/sse-token"); // default: slug-routed (the bug)
    expect(apiUrl("/api/sse-token", { host: true })).not.toContain("/agents/"); // the fix
  });
});

describe("frameIsForeign — cross-context stream guard (subagent-stream-isolation #1394 follow-up)", () => {
  // A console turn streams exactly one A2A context (its sessionId); the SDK stamps every frame
  // with its originating contextId, so a frame from a DIFFERENT context is cross-talk to drop.
  // A frame with no contextId is never foreign (back-compat / A2A 0.3 flat shape).
  const SESSION = "sess-1";

  it("drops an artifactUpdate stamped with a different contextId (e.g. a background job)", () => {
    const frame = { result: { artifactUpdate: { taskId: "t9", contextId: "background:bg-7", artifact: { parts: [] } } } };
    expect(frameIsForeign(frame, SESSION)).toBe(true);
  });

  it("keeps an artifactUpdate stamped with this turn's contextId", () => {
    const frame = { result: { artifactUpdate: { taskId: "t1", contextId: SESSION, artifact: { parts: [] } } } };
    expect(frameIsForeign(frame, SESSION)).toBe(false);
  });

  it("keeps an artifactUpdate with NO contextId (older server → guard is a no-op)", () => {
    const frame = { result: { artifactUpdate: { taskId: "t1", artifact: { parts: [] } } } };
    expect(frameIsForeign(frame, SESSION)).toBe(false);
  });

  it("drops foreign statusUpdate and task frames; keeps matching ones", () => {
    expect(frameIsForeign({ result: { statusUpdate: { taskId: "t9", contextId: "other" } } }, SESSION)).toBe(true);
    expect(frameIsForeign({ result: { task: { id: "t9", contextId: "other" } } }, SESSION)).toBe(true);
    expect(frameIsForeign({ result: { statusUpdate: { taskId: "t1", contextId: SESSION } } }, SESSION)).toBe(false);
    expect(frameIsForeign({ result: { task: { id: "t1", contextId: SESSION } } }, SESSION)).toBe(false);
  });

  it("handles the A2A 0.3 flat shape via result.contextId", () => {
    expect(frameIsForeign({ result: { kind: "artifact-update", contextId: "other" } }, SESSION)).toBe(true);
    expect(frameIsForeign({ result: { kind: "artifact-update", contextId: SESSION } }, SESSION)).toBe(false);
  });

  it("never treats an empty / resultless frame as foreign", () => {
    expect(frameIsForeign({}, SESSION)).toBe(false);
    expect(frameIsForeign({ result: {} }, SESSION)).toBe(false);
  });
});

// ── Background report by-id fetch (ADR 0070 D4) ────────────────────────────────
// api.backgroundJob hits GET /api/background/{id} (the only route carrying the FULL
// result); loadBackgroundReport wraps it for the report card / document viewer with
// a legacy list-and-filter fallback that fires ONLY on a 404.

const JOB = "bg-abcdefabcdef";
const GONE = /no longer available/;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Stub global fetch with a per-URL router; returns the list of requested paths. */
function stubFetch(route: (path: string) => Response) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      calls.push(path);
      return route(path);
    }),
  );
  return calls;
}

describe("api.backgroundJob / loadBackgroundReport (ADR 0070 D4)", () => {
  beforeEach(() => {
    // The apiUrl slug-routing suite above leaves the jsdom URL focused on a member
    // (/app/agent/m/) — reset to the host window so paths aren't /agents/m/-prefixed.
    window.history.replaceState({}, "", "/app/");
  });
  afterEach(() => vi.unstubAllGlobals());

  it("backgroundJob GETs the by-id route and returns the full row", async () => {
    const calls = stubFetch(() =>
      json({ id: JOB, status: "completed", subagent_type: "researcher", description: "dig", result: "the FULL report" }),
    );
    const job = await api.backgroundJob(JOB);
    expect(calls).toEqual([`/api/background/${JOB}`]);
    expect(job.id).toBe(JOB);
    expect(job.result).toBe("the FULL report");
  });

  it("loadBackgroundReport resolves the by-id result without touching the list", async () => {
    const calls = stubFetch(() => json({ id: JOB, status: "completed", result: "full text" }));
    await expect(loadBackgroundReport(JOB)).resolves.toBe("full text");
    expect(calls).toEqual([`/api/background/${JOB}`]);
  });

  it("falls back to list-and-filter ONLY on a 404 (pre-ADR-0070 server)", async () => {
    const calls = stubFetch((path) =>
      path === `/api/background/${JOB}`
        ? json({ detail: "not found" }, 404)
        : json({ enabled: true, jobs: [{ id: JOB, status: "completed", result: "from the list" }] }),
    );
    await expect(loadBackgroundReport(JOB)).resolves.toBe("from the list");
    expect(calls).toEqual([`/api/background/${JOB}`, "/api/background"]);
  });

  it("404 + job absent from the list → the 'no longer available' placeholder", async () => {
    stubFetch((path) =>
      path === `/api/background/${JOB}` ? json({ detail: "gone" }, 404) : json({ enabled: true, jobs: [] }),
    );
    await expect(loadBackgroundReport(JOB)).resolves.toMatch(GONE);
  });

  it("a completed row with an empty result also reads as unavailable", async () => {
    stubFetch(() => json({ id: JOB, status: "completed", result: "" }));
    await expect(loadBackgroundReport(JOB)).resolves.toMatch(GONE);
  });

  it("non-404 failures PROPAGATE (no silent fallback that hides a real error)", async () => {
    const calls = stubFetch(() => json({ detail: "boom" }, 500));
    await expect(loadBackgroundReport(JOB)).rejects.toMatchObject({ status: 500 });
    expect(calls).toEqual([`/api/background/${JOB}`]); // never reached the list
  });
});

// ── SOUL.md version history (#1691) ────────────────────────────────────────────
// The three routes the version-history panel drives: list, fetch-one, restore.
describe("api soul version history (#1691)", () => {
  beforeEach(() => window.history.replaceState({}, "", "/app/")); // host window, no slug prefix
  afterEach(() => vi.unstubAllGlobals());

  it("soulHistory GETs the history list route", async () => {
    const calls = stubFetch(() => json({ versions: [] }));
    await expect(api.soulHistory()).resolves.toEqual({ versions: [] });
    expect(calls).toEqual(["/api/config/soul/history"]);
  });

  it("soulVersion GETs one version and URL-encodes the id", async () => {
    const calls = stubFetch(() => json({ id: "a/b", content: "# persona" }));
    const res = await api.soulVersion("a/b");
    expect(res.content).toBe("# persona");
    expect(calls).toEqual(["/api/config/soul/history/a%2Fb"]); // encodeURIComponent, not a path split
  });

  it("restoreSoulVersion POSTs to the restore route", async () => {
    const seen: { path: string; method?: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        seen.push({ path: String(input), method: init?.method });
        return json({ ok: true, messages: ["reloaded"], restored: "20260101T000000.000000Z-abcd1234" });
      }),
    );
    const res = await api.restoreSoulVersion("20260101T000000.000000Z-abcd1234");
    expect(res.ok).toBe(true);
    expect(res.messages).toEqual(["reloaded"]);
    expect(seen[0].path).toBe("/api/config/soul/history/20260101T000000.000000Z-abcd1234/restore");
    expect(seen[0].method).toBe("POST");
  });
});
