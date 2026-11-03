// Plugin-view deep links: a console URL that opens one plugin rail view on load and hands
// that view some query parameters.
//
//   <console>/?plugin=<plugin_id>[&view=<view_id>][&vp=<urlencoded query>]
//
// e.g. a QR label on a storage bin:
//   https://hub.example.ts.net/app/agent/merchantAgent-6604/?plugin=inventory&view=view&vp=bin%3DB07
//
// The names mirror the `ui.navigate` host intent (`{plugin, view}`, ADR 0044) so a link and a
// plugin's own `registry.navigate(view)` resolve the same way: a blank/absent `view` means
// that plugin's first navigable view. `vp` ("view params") is appended to the iframe's src,
// so the plugin page reads it from its OWN `location.search` — it never touches the console.
//
// Safety: the link names a plugin + view; the PATH comes from the runtime's declared view
// (same-origin, ADR 0026), never from the URL — so there is no open redirect. `vp` is parsed
// and re-serialized by URLSearchParams, which percent-encodes `/ ? #`, so it can only ever
// add query data to that path. The server mints these with `sdk.view_url(...)`.

export const DEEP_LINK_KEYS = ["plugin", "view", "vp"] as const;

// Same shape as the manifest's plugin-id rule (graph/plugins/manifest.py _VALID_PLUGIN_ID);
// view ids are free-form in manifests, so allow the usual slug characters plus `.`.
const PLUGIN_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;
const VIEW_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/;
// A QR code or a pasted link — a few hundred bytes is plenty; cap so a hostile link can't
// stuff an unbounded query into a plugin page.
export const MAX_VIEW_QUERY = 2048;

export type ViewDeepLink = {
  plugin: string;
  view: string; // "" = the plugin's first navigable view
  query: string; // normalised `a=1&b=2`, "" when none
};

/** Parse a `location.search` string. Returns null when there is no deep link (no `plugin`).
 *  Returns `{error}` for a link that is present but malformed, so the console can say so. */
export function parseViewDeepLink(search: string): ViewDeepLink | { error: string } | null {
  let q: URLSearchParams;
  try {
    q = new URLSearchParams(search);
  } catch {
    return null;
  }
  const plugin = (q.get("plugin") ?? "").trim();
  if (!q.has("plugin")) return null;
  if (!PLUGIN_ID.test(plugin)) return { error: "That link names a plugin id the console can't read." };
  const view = (q.get("view") ?? "").trim();
  if (view && !VIEW_ID.test(view)) return { error: "That link names a view id the console can't read." };
  const raw = q.get("vp") ?? "";
  let query = "";
  if (raw) {
    try {
      query = new URLSearchParams(raw.replace(/^\?/, "")).toString();
    } catch {
      query = "";
    }
    if (query.length > MAX_VIEW_QUERY) return { error: "That link carries too much data to open." };
  }
  return { plugin, view, query };
}

/** `search` with the deep-link keys removed, keeping every other param (e.g. `__apiPort`,
 *  feature flags). Returns "" or a string starting with `?`. */
export function stripViewDeepLink(search: string): string {
  const q = new URLSearchParams(search);
  for (const k of DEEP_LINK_KEYS) q.delete(k);
  const rest = q.toString();
  return rest ? `?${rest}` : "";
}

/** Append a normalised view query to a view path that may already carry a query
 *  (a view-tab path like `/plugins/x/board?tab=open`). */
export function withViewQuery(path: string, query: string | undefined): string {
  if (!query) return path;
  const hash = path.indexOf("#");
  const base = hash >= 0 ? path.slice(0, hash) : path;
  const frag = hash >= 0 ? path.slice(hash) : "";
  return `${base}${base.includes("?") ? "&" : "?"}${query}${frag}`;
}

/** Resolve the link against the navigable plugin views (keys `plugin:<id>:<view>`):
 *  the named view, or the plugin's first one when `view` is blank. */
export function resolveViewDeepLink<V extends { key: string }>(link: ViewDeepLink, views: V[]): V | null {
  const own = views.filter((v) => v.key.startsWith(`plugin:${link.plugin}:`));
  if (!link.view) return own[0] ?? null;
  return own.find((v) => v.key === `plugin:${link.plugin}:${link.view}`) ?? null;
}
