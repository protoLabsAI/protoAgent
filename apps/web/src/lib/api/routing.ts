/**
 * Slug routing + auth for the console's HTTP calls (ADR 0042), split out of `lib/api.ts`
 * (#3808). Leaf module: imports nothing from `lib/api.ts`, so `http.ts` and `api.ts` can
 * both depend on it without a cycle. `lib/api.ts` re-exports the public names.
 */

function defaultApiBase() {
  if (typeof window === "undefined") return "";
  let savedBase = "";
  try {
    savedBase = window.localStorage.getItem("protoagent.apiBase") || "";
  } catch {
    savedBase = "";
  }
  if (savedBase) return savedBase.replace(/\/$/, "");

  // The Tauri desktop shell boots its bundled server on a dynamically-chosen
  // free port and hands it to the webview two ways (lib.rs): a `window` global,
  // and `?__apiPort=` on the URL. The URL is always visible to the page (the
  // global sometimes isn't, in which case we'd otherwise fall back to a dead
  // legacy port → "Load failed"). Try the URL first, then the global.
  try {
    const p = new URLSearchParams(window.location.search).get("__apiPort");
    if (p && /^\d+$/.test(p)) return `http://127.0.0.1:${p}`;
  } catch {
    /* no-op */
  }
  const injected = (window as unknown as { __PROTOAGENT_API_BASE__?: string })
    .__PROTOAGENT_API_BASE__;
  if (injected) return injected.replace(/\/$/, "");

  const { hostname, protocol } = window.location;
  if (protocol === "tauri:" || protocol === "file:" || hostname === "tauri.localhost") {
    return "http://127.0.0.1:7870";
  }
  return "";
}

// Fleet slug routing (ADR 0042). The focused agent lives in the URL — /app/agent/<slug>/ —
// so each console window targets its own agent: deterministic, survives reload, and two
// agents can be open in two windows at once. apiUrl() reads that slug and routes agent-level
// calls through the hub's per-agent proxy (/agents/<slug>/api/*). `host` (or no slug) = this
// instance, talking to /api directly. Hub control-plane paths (the fleet itself) are never
// scoped — they're served by the supervisor.
export function currentSlug(): string {
  try {
    const m = window.location.pathname.match(/\/agent\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : "host";
  } catch {
    return "host";
  }
}

/** True when this window is the host console (the un-suffixed root or the reserved
 *  `host` slug) — the only console allowed to edit the box-shared Global defaults
 *  (ADR 0047 §7.7). A workspace console sees those fields read-only. */
export function isHostConsole(): boolean {
  return currentSlug() === "host";
}

/** URL of the console focused on `slug` (for navigation / opening a new window). */
export function agentHref(slug: string): string {
  const base = import.meta.env.BASE_URL || "/"; // "/app/"
  return slug === "host" ? base : `${base}agent/${encodeURIComponent(slug)}/`;
}

function isHubPath(path: string) {
  // The fleet control plane is served by the supervisor itself — never scoped to an agent.
  return path.startsWith("/api/fleet") || path.startsWith("/api/archetypes");
}
export function isAgentPath(path: string) {
  // Everything that drives the focused AGENT: its console API, its A2A brain (streaming chat),
  // its OpenAI-compat endpoint, and its plugin VIEW content. /api/fleet stays on the hub.
  //
  // `/plugins/` is the registry's DEFAULT router prefix — plugin views served there (e.g.
  // agent_browser → /plugins/agent_browser/panel) are the focused agent's, so a fleet member's
  // view must proxy to it. Custom-prefix plugins serve their view at /api/plugins/<id>/… (already
  // covered by the /api/ clause). Without /plugins/ here, a member's default-prefix view iframe
  // hits the hub origin instead of the member → 404 (the agent_browser/project_board panels).
  //
  // `/media/` is the core media store (#1929 `registry.save_media` → `GET /media/<file>?sig=…`)
  // — a media file a member's tool generated lives on the MEMBER, so its console view must
  // proxy there too (#1946). The hub-side proxy is a catch-all (fleet_routes.py
  // `/agents/{slug}/{path:path}`), so no server change is needed.
  return (
    (path.startsWith("/api/") && !isHubPath(path)) ||
    path.startsWith("/plugins/") ||
    path.startsWith("/media/") ||
    path.startsWith("/a2a") ||
    path.startsWith("/v1")
  );
}

export function apiUrl(path: string, opts?: { host?: boolean }) {
  if (/^https?:\/\//.test(path)) return path;
  // Agent-level paths route through the focused agent's proxy, keyed by the URL slug.
  // `opts.host` forces the HUB (no slug routing) — for origin-level reads (the tenant
  // uid) that must stay on the hub regardless of which agent is focused.
  let p = path;
  const slug = currentSlug();
  if (!opts?.host && slug !== "host" && isAgentPath(path)) {
    p = `/agents/${encodeURIComponent(slug)}${path}`;
  }
  const base = defaultApiBase();
  return base ? `${base}${p.startsWith("/") ? p : `/${p}`}` : p;
}

/** Absolute URL for `rel` on a SPECIFIC fleet member, via the hub's per-agent proxy —
 *  independent of which window is focused. `slug` is the member id, or "host" for this
 *  instance. apiUrl() routes to the CURRENT window's slug; this targets an arbitrary
 *  member (Fleet Room DMs + broadcast). */
export function memberPath(slug: string, rel: string): string {
  const base = defaultApiBase();
  const p = slug === "host" ? rel : `/agents/${encodeURIComponent(slug)}${rel}`;
  return base ? `${base}${p.startsWith("/") ? p : `/${p}`}` : p;
}

/** Operator bearer token, set in localStorage (`protoagent.authToken`). Sent on
 * every fetch-based API + A2A call so a token-configured deployment's console
 * authenticates against the server guard. Blank ⇒ no header — the default
 * local/desktop case (no token) stays open. (The `/api/events` EventSource is
 * exempt server-side since EventSource can't set headers.) */
export function authToken(): string {
  try {
    return window.localStorage.getItem("protoagent.authToken") || "";
  } catch {
    return "";
  }
}

export function applyAuth(headers: Headers): Headers {
  const t = authToken();
  if (t) headers.set("Authorization", `Bearer ${t}`);
  return headers;
}
