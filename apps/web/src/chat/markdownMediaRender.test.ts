// Render-level proof for #1946: mount the REAL <Markdown> (DS → streamdown pipeline) and
// assert the <img>/<a> that reach the DOM carry the rewritten URL — i.e. the DS actually
// applies the consumer rehypePlugins after its sanitize/harden defaults. The pure rewrite
// logic is covered in mediaUrls.test.ts; this covers the wiring.
import { afterEach, describe, expect, it } from "vitest";
import { act } from "react";
import { createElement } from "react";
import { createRoot, type Root } from "react-dom/client";

import { Markdown } from "./Markdown";

// React's act() warning gate — the supported way to drive commits in a non-test-renderer env.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(md: string): Promise<HTMLElement> {
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => {
    root = createRoot(host!);
    root.render(createElement(Markdown, null, md));
  });
  return host;
}

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  window.history.replaceState({}, "", "/app/");
});

describe("<Markdown> media URL rewrite reaches the DOM (#1946)", () => {
  it("desktop shell: an embedded /media/ image renders with the sidecar base + intact ?sig", async () => {
    window.history.replaceState({}, "", "/app/?__apiPort=54321");
    const el = await render("here you go\n\n![chart](/media/chart.png?sig=abc123)");
    const img = el.querySelector("img");
    expect(img?.getAttribute("src")).toBe("http://127.0.0.1:54321/media/chart.png?sig=abc123");
  });

  it("fleet member view: the image proxies via /agents/<slug>/ and a /media/ link follows", async () => {
    window.history.replaceState({}, "", "/app/agent/ava/");
    const el = await render("![x](/media/a.png?sig=s) and [the file](/media/a.png?sig=s)");
    expect(el.querySelector("img")?.getAttribute("src")).toBe("/agents/ava/media/a.png?sig=s");
    expect(el.querySelector("a")?.getAttribute("href")).toBe("/agents/ava/media/a.png?sig=s");
  });

  it("same-origin host console: src is byte-for-byte unchanged; external links untouched", async () => {
    window.history.replaceState({}, "", "/app/");
    const el = await render("![x](/media/a.png?sig=s) [ext](https://example.com/y)");
    expect(el.querySelector("img")?.getAttribute("src")).toBe("/media/a.png?sig=s");
    expect(el.querySelector('a[href="https://example.com/y"]')).toBeTruthy();
  });
});
