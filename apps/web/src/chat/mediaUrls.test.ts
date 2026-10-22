import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  absolutizeServerUrl,
  handleMediaImageError,
  rehypeAbsolutizeServerUrls,
  remintEndpoint,
} from "./mediaUrls";

// absolutizeServerUrl rides apiUrl(): the slug comes from the URL (/app/agent/<slug>/)
// and the desktop base from ?__apiPort= — drive both via history, like api.test.ts.
const focus = (path: string) => window.history.replaceState({}, "", path);

afterEach(() => focus("/app/"));

describe("absolutizeServerUrl (#1946)", () => {
  it("same-origin host console: /media/ + /plugins/ are IDENTITY (unchanged behavior)", () => {
    focus("/app/");
    expect(absolutizeServerUrl("/media/chart.png?sig=abc123")).toBe("/media/chart.png?sig=abc123");
    expect(absolutizeServerUrl("/plugins/banana/out.png")).toBe("/plugins/banana/out.png");
  });

  it("desktop shell (?__apiPort=): rewrites against the sidecar's dynamic-port base", () => {
    // The #1946 repro: the Tauri webview's origin isn't the agent server, so a
    // root-relative /media/ URL resolved against the wrong origin → "Image not available".
    focus("/app/?__apiPort=54321");
    expect(absolutizeServerUrl("/media/chart.png?sig=abc123")).toBe(
      "http://127.0.0.1:54321/media/chart.png?sig=abc123",
    );
  });

  it("fleet member window: routes /media/ through the hub's /agents/<slug>/ proxy", () => {
    focus("/app/agent/ava/");
    expect(absolutizeServerUrl("/media/chart.png?sig=abc123")).toBe(
      "/agents/ava/media/chart.png?sig=abc123",
    );
  });

  it("the signed query (?sig=…) survives the rewrite verbatim", () => {
    focus("/app/agent/ava/?__apiPort=54321");
    const out = absolutizeServerUrl("/media/f.png?sig=deadbeef&exp=99");
    expect(out.endsWith("/media/f.png?sig=deadbeef&exp=99")).toBe(true);
  });

  it("leaves absolute URLs, data: URIs, anchors, and other relative paths alone", () => {
    focus("/app/agent/ava/?__apiPort=54321"); // even with every rewrite trigger active
    for (const url of [
      "https://example.com/media/x.png",
      "data:image/png;base64,AAAA",
      "#section",
      "/static/logo.png",
      "media/relative.png",
    ]) {
      expect(absolutizeServerUrl(url)).toBe(url);
    }
  });
});

describe("rehypeAbsolutizeServerUrls — the hast-tree rewrite behind <Markdown> (#1946)", () => {
  type Node = {
    type?: string;
    tagName?: string;
    properties?: { src?: unknown; href?: unknown };
    children?: Node[];
  };

  const img = (src: unknown): Node => ({ type: "element", tagName: "img", properties: { src } });
  const run = (tree: Node) => {
    rehypeAbsolutizeServerUrls()(tree);
    return tree;
  };

  it("rewrites img[src] and a[href] anywhere in the tree, leaving other URLs alone", () => {
    focus("/app/agent/ava/");
    const link: Node = {
      type: "element",
      tagName: "a",
      properties: { href: "/media/report.pdf?sig=s1" },
    };
    const external: Node = {
      type: "element",
      tagName: "a",
      properties: { href: "https://example.com/x" },
    };
    // The image nests inside a paragraph — the walk must recurse.
    const tree: Node = {
      type: "root",
      children: [{ type: "element", tagName: "p", children: [img("/media/a.png?sig=s2"), link, external] }],
    };
    run(tree);
    const p = tree.children![0];
    expect(p.children![0].properties!.src).toBe("/agents/ava/media/a.png?sig=s2");
    expect(p.children![1].properties!.href).toBe("/agents/ava/media/report.pdf?sig=s1");
    expect(p.children![2].properties!.href).toBe("https://example.com/x");
  });

  it("ignores non-string src (streamdown may strip a sanitized attribute) and non-elements", () => {
    focus("/app/agent/ava/");
    const tree: Node = {
      type: "root",
      children: [img(undefined), { type: "text" }, { type: "element", tagName: "img" }],
    };
    expect(() => run(tree)).not.toThrow();
    expect(tree.children![0].properties!.src).toBeUndefined();
  });

  it("is a full no-op on the same-origin host console", () => {
    focus("/app/");
    const tree: Node = { type: "root", children: [img("/media/a.png?sig=s")] };
    run(tree);
    expect(tree.children![0].properties!.src).toBe("/media/a.png?sig=s");
  });
});

describe("remintEndpoint — which agent owns the expired link (#4274)", () => {
  it("a member link (/agents/<slug>/media/, relative or absolute) → that member's endpoint", () => {
    expect(remintEndpoint("/agents/ava/media/a.png?exp=1&sig=s")).toBe("/agents/ava/api/media/remint");
    expect(remintEndpoint("http://127.0.0.1:54321/agents/ava/media/a.png?exp=1&sig=s")).toBe(
      "/agents/ava/api/media/remint",
    );
  });

  it("a hub-local /media/ link (relative or absolute) → this agent's endpoint", () => {
    expect(remintEndpoint("/media/a.png?exp=1&sig=s")).toBe("/api/media/remint");
    expect(remintEndpoint("http://127.0.0.1:54321/media/a.png?exp=1&sig=s")).toBe("/api/media/remint");
  });

  it("a non-media image → null (nothing to re-mint)", () => {
    expect(remintEndpoint("https://example.com/photo.jpg")).toBeNull();
    expect(remintEndpoint("/static/logo.png")).toBeNull();
    expect(remintEndpoint("data:image/png;base64,AAAA")).toBeNull();
  });
});

describe("handleMediaImageError — re-mint an expired link on load error (#4274)", () => {
  // The re-mint rides the console's real authed `request()` helper; mock `fetch` under it so the
  // tests see the exact URL + body it issues (host console ⇒ `apiUrl()` is identity), with no net.
  let fetchMock: ReturnType<typeof vi.fn>;
  const ok = (url: string) => ({ ok: true, status: 200, json: async () => ({ url }), text: async () => "" });
  const notFound = () => ({
    ok: false,
    status: 404,
    statusText: "Not Found",
    json: async () => ({}),
    text: async () => "{}",
  });

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  const imgWith = (src: string): HTMLImageElement => {
    const el = document.createElement("img");
    el.setAttribute("src", src);
    return el;
  };
  // Drain the microtask chain (request → swap src) behind a real macrotask.
  const flush = () => new Promise((r) => setTimeout(r, 0));
  const posted = (i: number) => {
    const [url, init] = fetchMock.mock.calls[i] as [RequestInfo, RequestInit];
    return { url: String(url), method: init.method, body: JSON.parse(init.body as string) };
  };

  it("r1: an expired member link POSTs once to /agents/<slug>/api/media/remint {url}, then swaps src", async () => {
    fetchMock.mockResolvedValueOnce(ok("/agents/ava/media/a.png?exp=2&sig=fresh"));
    const el = imgWith("/agents/ava/media/a.png?exp=1&sig=old1");
    handleMediaImageError(el);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(posted(0)).toEqual({
      url: "/agents/ava/api/media/remint",
      method: "POST",
      body: { url: "/agents/ava/media/a.png?exp=1&sig=old1" },
    });
    expect(el.getAttribute("src")).toBe("/agents/ava/media/a.png?exp=2&sig=fresh");
  });

  it("r2: a hub-local /media/ link POSTs to /api/media/remint", async () => {
    fetchMock.mockResolvedValueOnce(ok("/media/b.png?exp=2&sig=fresh"));
    const el = imgWith("/media/b.png?exp=1&sig=old2");
    handleMediaImageError(el);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(posted(0)).toEqual({
      url: "/api/media/remint",
      method: "POST",
      body: { url: "/media/b.png?exp=1&sig=old2" },
    });
    expect(el.getAttribute("src")).toBe("/media/b.png?exp=2&sig=fresh");
  });

  it("r3: a 404 leaves the image untouched, and a second error does not POST again", async () => {
    fetchMock.mockResolvedValueOnce(notFound());
    const el = imgWith("/media/c.png?exp=1&sig=old3");
    handleMediaImageError(el);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(el.getAttribute("src")).toBe("/media/c.png?exp=1&sig=old3"); // unchanged
    handleMediaImageError(el); // the broken image fires error again
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1); // per-element guard: no second POST
  });

  it("r4: two images sharing one original URL re-mint with a single POST (per-URL cache)", async () => {
    fetchMock.mockResolvedValueOnce(ok("/media/d.png?exp=2&sig=fresh"));
    const url = "/media/d.png?exp=1&sig=old4";
    const a = imgWith(url);
    const b = imgWith(url);
    handleMediaImageError(a);
    await flush();
    handleMediaImageError(b);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(a.getAttribute("src")).toBe("/media/d.png?exp=2&sig=fresh");
    expect(b.getAttribute("src")).toBe("/media/d.png?exp=2&sig=fresh");
  });

  it("r5: a non-media image never POSTs and is left unmarked", async () => {
    const el = imgWith("https://example.com/photo.jpg");
    handleMediaImageError(el);
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(el.dataset.remintTried).toBeUndefined();
  });
});
