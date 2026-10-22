import { apiUrl } from "../lib/api";
import { request } from "../lib/api/http";

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

// Re-mint of an EXPIRED media link in chat history (#4274 item 4). A member's inline images
// carry expiring signed links (`…/media/<file>?exp=…&sig=…`, `media.remote_link_ttl_s`, default
// 7 days); once they lapse the `<img>` 403s and old history loses its images. The server half
// (bd-9yuj) answers `POST /api/media/remint {url}` → `{url}` with a fresh link for the SAME file,
// 404 when it can't be re-minted. Through the hub it's `POST /agents/<slug>/api/media/remint`.
//
// On the FIRST load error of a media `<img>`, we re-mint through the endpoint that owns the link
// and swap `src` to the fresh URL. A 404 / any failure leaves the broken image (the DS renders its
// own "Image not available" fallback) — no loop, no toast. The signed `?sig=` is never read here.

/** The remint endpoint that owns a media link, or null when `src` isn't a `/media/` link at all
 *  (a non-media image, which never re-mints). A member link carries `/agents/<slug>/media/` in its
 *  path → that member's `/agents/<slug>/api/media/remint`; a hub-local `/media/` link → this
 *  agent's `/api/media/remint` (slug-routed by `request()`/`apiUrl()` to the focused window). Reads
 *  only the PATH — the signed query (`…?sig=`) is never parsed here, so it can never be logged. */
export function remintEndpoint(src: string): string | null {
  let path: string;
  try {
    path = new URL(src, window.location.origin).pathname;
  } catch {
    return null;
  }
  const member = path.match(/\/agents\/([^/]+)\/media\//);
  if (member) return `/agents/${member[1]}/api/media/remint`;
  if (path.startsWith("/media/")) return "/api/media/remint";
  return null;
}

// Per-page-session cache: original media URL → the pending re-mint (fresh URL, or null when it
// 404'd / failed). Keyed by the as-rendered URL so a re-render of the same history never re-POSTs,
// and two images sharing one original coalesce onto a single request.
const remintCache = new Map<string, Promise<string | null>>();

/** onError handler for a chat media `<img>`: on the FIRST error for this element, re-mint the
 *  expired link and swap in the fresh URL. At most one attempt per element (a 404 or a second
 *  error never re-POSTs); results are cached per original URL for the page session. A non-media
 *  image gets this handler too but never POSTs. */
export function handleMediaImageError(img: HTMLImageElement): void {
  const original = img.getAttribute("src") || "";
  const endpoint = remintEndpoint(original);
  if (!endpoint) return; // non-media image — never POST
  let pending = remintCache.get(original);
  if (!pending) {
    if (img.dataset.remintTried) return; // at most one POST per image element
    img.dataset.remintTried = "1";
    pending = request<{ url?: string }>(endpoint, { method: "POST", body: { url: original } })
      .then((r) => (typeof r?.url === "string" && r.url ? r.url : null))
      // 404 (can't re-mint) or any other failure: leave the broken image, no retry, no toast.
      .catch(() => null);
    remintCache.set(original, pending);
  }
  // Swap in the fresh URL — re-applied on a cache hit too, so if a re-render reverts the <img>
  // back to the expired src (React owns `src`), the next error repaints the fresh link without a
  // second POST. `!== fresh` avoids a redundant write (and never re-mints the fresh link itself).
  void pending.then((fresh) => {
    if (fresh && img.getAttribute("src") !== fresh) img.src = fresh;
  });
}
