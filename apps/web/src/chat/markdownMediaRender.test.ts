// Render-level proof for #1946: mount the REAL <Markdown> (DS → streamdown pipeline) and
// assert the <img>/<a> that reach the DOM carry the rewritten URL — i.e. the DS actually
// applies the consumer rehypePlugins after its sanitize/harden defaults. The pure rewrite
// logic is covered in mediaUrls.test.ts; this covers the wiring.
import { afterEach, describe, expect, it, vi } from "vitest";
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
  vi.unstubAllGlobals();
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

describe("<Markdown> re-mints an expired media image on load error (#4274)", () => {
  it("a member image that 403s POSTs to the member's remint endpoint and repaints the fresh link", async () => {
    window.history.replaceState({}, "", "/app/agent/ava/");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ url: "/agents/ava/media/old.png?exp=2&sig=fresh" }),
      text: async () => "",
    });
    vi.stubGlobal("fetch", fetchMock);

    const el = await render("![chart](/media/old.png?exp=1&sig=stale)");
    const img = el.querySelector("img")!;
    // #1946 rewrite already pointed it at the member proxy.
    expect(img.getAttribute("src")).toBe("/agents/ava/media/old.png?exp=1&sig=stale");

    // img load errors don't bubble — the capture-phase listener on the wrapper catches it.
    await act(async () => {
      img.dispatchEvent(new Event("error"));
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [RequestInfo, RequestInit];
    expect(String(url)).toBe("/agents/ava/api/media/remint");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ url: "/agents/ava/media/old.png?exp=1&sig=stale" });
    expect(img.getAttribute("src")).toBe("/agents/ava/media/old.png?exp=2&sig=fresh");
  });
});
