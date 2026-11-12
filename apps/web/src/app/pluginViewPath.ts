// A plugin view's `path` is DATA — off a runtime-status response, which for a remote fleet
// member is whatever that member's server chose to send. `apiUrl()` returns an absolute URL
// verbatim, and the view frame is handed the console's bearer (the reachability probe's
// Authorization header, then the `protoagent:init` post, targeted at the frame's own origin).
// So a path with a scheme or host is not "a view somewhere else": it is this console's
// operator token delivered to that somewhere else. Such a view is never mounted.
//
// Same rule the server logs for at manifest parse (`_NON_SAME_ORIGIN_PATH`) and the palette
// enforces for routes (`pluginPaletteCommands`) — here it is enforced, because a remote
// member's views never pass through this hub's manifest parser.

// eslint-disable-next-line no-control-regex
const URL_RESHAPING_CHARS = /[\u0000-\u001f\u007f]/;
// A backslash is a slash to the URL parser ("/\\host" is "//host").
const NON_SAME_ORIGIN = /https?:\/\/|^\/\/|\\|localhost|:\d/i;

/** Whether `path` can only ever resolve to the focused agent's own server. */
export function isSameOriginViewPath(path: unknown): path is string {
  if (typeof path !== "string" || !path.trim()) return false;
  if (URL_RESHAPING_CHARS.test(path)) return false;
  // Judge the path itself, not a query a deep link appended to it.
  const bare = path.split(/[?#]/, 1)[0];
  if (/^[a-z][a-z0-9+.-]*:/i.test(bare.trim())) return false; // any scheme (javascript:, data:, …)
  return !NON_SAME_ORIGIN.test(bare);
}

/** Every path a view can mount: its own and each sub-tab's. */
export function viewPathsAreSameOrigin(view: { path?: unknown; tabs?: { path?: unknown }[] }): boolean {
  return isSameOriginViewPath(view.path) && (view.tabs ?? []).every((t) => isSameOriginViewPath(t.path));
}
