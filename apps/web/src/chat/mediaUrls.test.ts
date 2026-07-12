import { afterEach, describe, expect, it } from "vitest";

import { absolutizeServerUrl, rehypeAbsolutizeServerUrls } from "./mediaUrls";

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
