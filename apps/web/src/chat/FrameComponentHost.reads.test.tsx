// FrameComponentHost read channel (ADR 0118 D5 amendment, #4143): a frame component posts
// `protoComponent:read {cid, path}` and the host — never the frame — decides, fetches with the
// operator's auth, and answers `protoComponent:readResult {cid, ok, status, json|error}`.
// One `it` per host check: allowlisted path served; undeclared / other-plugin / POST refused;
// rate + in-flight limits; size cap; non-JSON refused; spoofed source ignored; paused while
// hidden; and the bearer never appears in any message to the frame.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FrameComponentHost, type FrameComponentHostProps } from "./FrameComponentHost";
import { checkReadRequest, createReadLimiter, matchesReadPattern, parseReadPath } from "./frameComponentReads";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const BEARER = "SECRET-BEARER-do-not-leak";
let container: HTMLElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  window.localStorage.setItem("protoagent.authToken", BEARER);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

function jsonResponse(body: unknown, init: { status?: number; contentType?: string } = {}): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { "content-type": init.contentType ?? "application/json" },
  });
}

function mountHost(extra: Partial<FrameComponentHostProps> = {}) {
  act(() =>
    root.render(
      h(FrameComponentHost, {
        id: "rx",
        frameUrl: "/plugins/demo/widget",
        kind: "pl-demo",
        plugin: "demo",
        props: {},
        reads: ["features", "features/*", "status"],
        ...extra,
      }),
    ),
  );
  const frame = container.querySelector("iframe") as HTMLIFrameElement;
  const post = vi.spyOn(frame.contentWindow as Window, "postMessage").mockImplementation(() => undefined as never);
  return { frame, post };
}

async function read(frame: HTMLIFrameElement, data: Record<string, unknown>, source?: unknown) {
  await act(async () => {
    const ev = new MessageEvent("message", { data: { type: "protoComponent:read", ...data } });
    Object.defineProperty(ev, "source", { value: source ?? frame.contentWindow, configurable: true });
    window.dispatchEvent(ev);
    // Let the fetch promise chain settle.
    await new Promise((r) => setTimeout(r, 0));
  });
}

type PostSpy = { mock: { calls: unknown[][] } };

function readResults(post: PostSpy): Record<string, unknown>[] {
  return post.mock.calls
    .map((c) => c[0] as Record<string, unknown>)
    .filter((m) => m?.type === "protoComponent:readResult");
}

function resultFor(post: PostSpy, cid: number): Record<string, unknown> | undefined {
  return readResults(post).find((m) => m.cid === cid);
}

describe("FrameComponentHost — read channel (#4143)", () => {
  it("serves an allowlisted path with the operator's auth and posts back only the JSON", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async () => jsonResponse({ id: "bd-x", status: "open" }));
    const { frame, post } = mountHost();
    await read(frame, { cid: 1, path: "features/bd-vbyd.24" });

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/plugins/demo/features/bd-vbyd.24");
    expect(init.method).toBe("GET");
    expect(init.redirect).toBe("error");
    expect((init.headers as Headers).get("Authorization")).toBe(`Bearer ${BEARER}`); // the HOST authenticates

    expect(resultFor(post, 1)).toEqual({
      type: "protoComponent:readResult",
      cid: 1,
      ok: true,
      status: 200,
      json: { id: "bd-x", status: "open" },
    });
    expect(post).toHaveBeenCalledWith(expect.objectContaining({ type: "protoComponent:readResult" }), "*");
    // An exact (non-wildcard) pattern is served too.
    await read(frame, { cid: 2, path: "status" });
    expect(resultFor(post, 2)?.ok).toBe(true);
  });

  it("never puts the bearer or any header into a message to the frame", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      const r = jsonResponse({ echoed: "ok" });
      r.headers.set("x-secret-header", "nope");
      return r;
    });
    const { frame, post } = mountHost();
    await read(frame, { cid: 1, path: "features" });
    await read(frame, { cid: 2, path: "nope/x" }); // a refusal message too
    expect(readResults(post)).toHaveLength(2);
    for (const [message] of post.mock.calls) {
      const s = JSON.stringify(message);
      expect(s).not.toContain(BEARER);
      expect(s.toLowerCase()).not.toContain("authorization");
      expect(s).not.toContain("x-secret-header");
      expect(Object.keys(message as object)).not.toContain("headers");
    }
  });

  it.each([
    ["an undeclared path", "secrets"],
    ["a deeper path than the one-segment wildcard", "features/x/secret-sub"],
    ["another plugin's route by traversal", "../other/features"],
    ["an absolute /api path", "/api/config"],
    ["an absolute own-prefix path", "/api/plugins/demo/features"],
    ["a query string", "features?limit=1"],
    ["a percent-encoded traversal", "features/%2e%2e"],
    ["an empty segment", "features//x"],
    ["a non-string path", 42],
  ])("refuses %s without fetching", async (_label, path) => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const { frame, post } = mountHost();
    await read(frame, { cid: 9, path });
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(resultFor(post, 9)).toEqual(expect.objectContaining({ cid: 9, ok: false, status: 0, error: expect.any(String) }));
  });

  it("refuses any method but GET", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const { frame, post } = mountHost();
    await read(frame, { cid: 3, path: "features", method: "POST" });
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(resultFor(post, 3)).toEqual(expect.objectContaining({ ok: false, error: "reads are GET only" }));
  });

  it("refuses every read when the kind declared none", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const { frame, post } = mountHost({ reads: [] });
    await read(frame, { cid: 4, path: "features" });
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(resultFor(post, 4)?.ok).toBe(false);
  });

  it("ignores a read from any window but its own frame", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const { frame, post } = mountHost();
    await read(frame, { cid: 5, path: "features" }, window);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(readResults(post)).toHaveLength(0);
  });

  it("caps reads in flight at two per frame", async () => {
    // Never settles, so the first two stay in flight.
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>(() => {}));
    const { frame, post } = mountHost();
    await read(frame, { cid: 1, path: "features" });
    await read(frame, { cid: 2, path: "features" });
    await read(frame, { cid: 3, path: "features" });
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(resultFor(post, 3)).toEqual(expect.objectContaining({ ok: false, error: expect.stringMatching(/in flight/) }));
  });

  it("caps reads at four per second per frame", async () => {
    let now = 10_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async () => jsonResponse({ ok: 1 }));
    const { frame, post } = mountHost();
    for (let cid = 1; cid <= 5; cid++) await read(frame, { cid, path: "status" });
    expect(fetchSpy).toHaveBeenCalledTimes(4);
    expect(resultFor(post, 5)).toEqual(expect.objectContaining({ ok: false, error: expect.stringMatching(/rate limited/) }));
    now += 1_001; // the window rolls over
    await read(frame, { cid: 6, path: "status" });
    expect(resultFor(post, 6)?.ok).toBe(true);
  });

  it("refuses a response over 256 KB", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ blob: "x".repeat(300 * 1024) }));
    const { frame, post } = mountHost();
    await read(frame, { cid: 1, path: "features" });
    const r = resultFor(post, 1)!;
    expect(r.ok).toBe(false);
    expect(r.error).toMatch(/too large/);
    expect(r).not.toHaveProperty("json");
  });

  it("refuses a non-JSON response and never forwards its body", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse("<html>secret page</html>", { contentType: "text/html" }),
    );
    const { frame, post } = mountHost();
    await read(frame, { cid: 1, path: "features" });
    const r = resultFor(post, 1)!;
    expect(r).toEqual(expect.objectContaining({ ok: false, status: 200, error: "response is not JSON" }));
    expect(JSON.stringify(r)).not.toContain("secret page");
  });

  it("reports a non-2xx status as ok:false with the status", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ detail: "no such feature" }, { status: 404 }));
    const { frame, post } = mountHost();
    await read(frame, { cid: 1, path: "features/missing" });
    expect(resultFor(post, 1)).toEqual(
      expect.objectContaining({ ok: false, status: 404, error: "no such feature" }),
    );
  });

  it("pauses servicing while the console tab is hidden", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({}));
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const { frame, post } = mountHost();
    await read(frame, { cid: 1, path: "features" });
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(resultFor(post, 1)).toEqual(expect.objectContaining({ ok: false, error: expect.stringMatching(/paused/) }));
  });

  it("pauses servicing while the frame is scrolled out of view", async () => {
    let cb: IntersectionObserverCallback | null = null;
    class MockIO {
      constructor(c: IntersectionObserverCallback) {
        cb = c;
      }
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords(): IntersectionObserverEntry[] {
        return [];
      }
    }
    const realIO = globalThis.IntersectionObserver;
    globalThis.IntersectionObserver = MockIO as unknown as typeof IntersectionObserver;
    try {
      const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async () => jsonResponse({}));
      act(() =>
        root.render(
          h(FrameComponentHost, {
            id: "vis",
            frameUrl: "/plugins/demo/widget",
            plugin: "demo",
            props: {},
            reads: ["features"],
          }),
        ),
      );
      // Near the viewport → the frame mounts (the lazy-mount observer, then the visibility one).
      act(() => cb!([{ isIntersecting: true } as IntersectionObserverEntry], null as never));
      act(() => cb!([{ isIntersecting: true } as IntersectionObserverEntry], null as never));
      const frame = container.querySelector("iframe") as HTMLIFrameElement;
      const post = vi.spyOn(frame.contentWindow as Window, "postMessage").mockImplementation(() => undefined as never);

      act(() => cb!([{ isIntersecting: false } as IntersectionObserverEntry], null as never));
      await read(frame, { cid: 1, path: "features" });
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(resultFor(post, 1)?.error).toMatch(/paused/);

      act(() => cb!([{ isIntersecting: true } as IntersectionObserverEntry], null as never));
      await read(frame, { cid: 2, path: "features" });
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      expect(resultFor(post, 2)?.ok).toBe(true);
    } finally {
      globalThis.IntersectionObserver = realIO;
    }
  });
});

describe("frameComponentReads — pure gates", () => {
  it("parses only safe relative paths", () => {
    expect(parseReadPath("features/bd-vbyd.24")).toEqual(["features", "bd-vbyd.24"]);
    for (const bad of ["", "/x", "../x", "a/./b", "a//b", "a?b", "a#b", "a%2fb", "a\\b", "a b", null, 1]) {
      expect(parseReadPath(bad)).toBeNull();
    }
    expect(parseReadPath("a/".repeat(300))).toBeNull(); // too long
  });

  it("matches `*` to exactly one segment", () => {
    expect(matchesReadPattern(["features", "x"], "features/*")).toBe(true);
    expect(matchesReadPattern(["features"], "features/*")).toBe(false);
    expect(matchesReadPattern(["features", "x", "y"], "features/*")).toBe(false);
    expect(matchesReadPattern(["status"], "status")).toBe(true);
    expect(matchesReadPattern(["statusx"], "status")).toBe(false);
  });

  it("checks method, plugin and allowlist", () => {
    expect(checkReadRequest({ path: "features" }, "demo", ["features"])).toEqual({ ok: true, path: "features" });
    expect(checkReadRequest({ path: "features", method: "get" }, "demo", ["features"]).ok).toBe(true);
    expect(checkReadRequest({ path: "features", method: "DELETE" }, "demo", ["features"]).ok).toBe(false);
    expect(checkReadRequest({ path: "features" }, null, ["features"]).ok).toBe(false);
    expect(checkReadRequest({ path: "features" }, "demo", undefined).ok).toBe(false);
  });

  it("limits starts per second and concurrent reads", () => {
    const lim = createReadLimiter(4, 2);
    expect(lim.acquire(0)).toBeNull();
    expect(lim.acquire(0)).toBeNull();
    expect(lim.acquire(0)).toMatch(/in flight/);
    lim.release();
    lim.release();
    expect(lim.acquire(10)).toBeNull();
    lim.release();
    expect(lim.acquire(20)).toBeNull();
    lim.release();
    expect(lim.acquire(30)).toMatch(/rate limited/);
    expect(lim.acquire(1001)).toBeNull();
  });
});
