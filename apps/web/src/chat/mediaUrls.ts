import { apiUrl } from "../lib/api";

// Server-relative URL rewriting for markdown replies (#1946). Replies embed core-served
// URLs — `![…](/media/<file>?sig=…)` from the #1929 `registry.save_media` → `ref.url`
// convention, plus `/plugins/<id>/…` assets a plugin serves itself. Root-relative URLs
// resolve against the PAGE origin, which is only correct in a same-origin browser console:
// the Tauri desktop shell serves the console from bundled assets (webview origin ≠ agent
// server) and a fleet member's console runs on the hub origin — both render
// "Image not available". `apiUrl()` already knows the right target for the focused agent
// (the desktop's dynamic-port base, the hub's /agents/<slug>/ proxy), so route the two
// server-owned prefixes through it before they reach the DOM.
//
// Two properties are load-bearing:
// - **Same-origin host console is a no-op by construction** — there `defaultApiBase()` is
//   "" and the slug is "host", so `apiUrl("/media/x")` returns "/media/x" verbatim.
// - **Signed queries survive** — the rewrite only PREFIXES the path, so `?sig=…` (the
//   HMAC that makes media work under a bearer gate) passes through untouched.
//
// Anything else — absolute URLs, data: URIs, anchors, other relative paths — is untouched.
const SERVER_PREFIXES = ["/media/", "/plugins/"];

/** Absolutize a server-owned root-relative URL against the focused agent; identity for
 *  everything else (and for the same-origin host console). */
export function absolutizeServerUrl(url: string): string {
  return SERVER_PREFIXES.some((p) => url.startsWith(p)) ? apiUrl(url) : url;
}

// Minimal hast shape — enough to rewrite img/src + a/href without a visitor dep (the same
// tiny-walk idiom as the DS Markdown's mermaid guard).
type HastNode = {
  type?: string;
  tagName?: string;
  properties?: { src?: unknown; href?: unknown };
  children?: unknown[];
};

/** rehype plugin: absolutize `/media/` + `/plugins/` URLs in `img[src]` and `a[href]`.
 *  A hast-tree rewrite (rather than a `components` override) keeps streamdown's built-in
 *  image chrome — the load-error fallback and hover download button — fully intact. The DS
 *  appends consumer plugins after its defaults, so this runs post-sanitize/harden and
 *  rewrites exactly what would otherwise reach the DOM. */
export function rehypeAbsolutizeServerUrls() {
  return (tree: unknown) => {
    const walk = (node: HastNode) => {
      if (!node || typeof node !== "object") return;
      if (node.type === "element" && node.properties) {
        if (node.tagName === "img" && typeof node.properties.src === "string") {
          node.properties.src = absolutizeServerUrl(node.properties.src);
        }
        if (node.tagName === "a" && typeof node.properties.href === "string") {
          node.properties.href = absolutizeServerUrl(node.properties.href);
        }
      }
      if (Array.isArray(node.children)) node.children.forEach((c) => walk(c as HastNode));
    };
    walk(tree as HastNode);
  };
}
