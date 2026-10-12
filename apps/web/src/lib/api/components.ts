/**
 * The live component-v1 catalog (ADR 0118 D5 — `GET /api/components`, #4087/S11b): the core
 * widget kinds plus every plugin-contributed kind, each as `{name, plugin, frame_url, reads}`. A
 * frame kind carries the public `/plugins/<id>/<frame>` page that renders it (`frame_url`);
 * core widgets and frame-less kinds carry null. The frame-component host (ADR 0118 S12)
 * resolves a component name to its frame page through here — the name→host resolution ORDER
 * is S12b, this slice only fetches, caches and resolves.
 *
 * Fetched ONCE and cached module-wide: the catalog only moves when plugins (re)load, so the
 * cache self-invalidates on the `plugin.#` bus topic — the same signal PluginChangeWatch
 * refreshes the rest of the plugin surface on. Framework-light (a plain fetch/cache plus a
 * small React hook) so S12b can consume either shape.
 *
 * One domain slice of the console `api` layer (#3822): it reaches the backend through
 * `request` from `./http` and NEVER imports `lib/api.ts` (that would be a cycle).
 */
import { useEffect, useState } from "react";

import { onTopic } from "../events";
import { parseErrorBody, request } from "./http";
import { apiUrl, applyAuth } from "./routing";

/** One row of `GET /api/components`: a renderable component-v1 kind. `plugin` is null for a
 *  core widget; `frame_url` is the public `/plugins/<id>/<frame>` page a frame kind renders
 *  in, else null (a core widget or a frame-less plugin kind). */
export type ComponentCatalogEntry = {
  name: string;
  plugin: string | null;
  frame_url: string | null;
  /** A frame kind's host-mediated read allowlist (ADR 0118 D5 amendment, #4143): GET path
   *  patterns RELATIVE to `/api/plugins/<plugin>/`, where `*` matches exactly one segment.
   *  Empty (or absent, from a pre-#4143 server) ⇒ the frame may read nothing. */
  reads?: string[];
};

const CATALOG_PATH = "/api/components";

let cached: Promise<ComponentCatalogEntry[]> | null = null;
let pluginWatch: (() => void) | null = null;
const listeners = new Set<() => void>();

// Subscribe to plugin (re)loads the first time the catalog is actually read, so merely
// importing this module never opens the SSE stream. A `plugin.#` frame means a kind may have
// appeared or dropped (a frame kind's validator rides the live plugin set), so the cache is
// dropped and every live consumer told to re-fetch.
function ensurePluginWatch(): void {
  if (pluginWatch) return;
  pluginWatch = onTopic("plugin.#", () => refreshComponentCatalog());
}

/** Fetch the component catalog, cached after the first call. `force` re-reads and replaces
 *  the cache. A failed fetch clears the cache (if it is still the cached entry) so the next call
 *  retries, rather than handing back a stale rejected promise forever. */
export function fetchComponentCatalog(force = false): Promise<ComponentCatalogEntry[]> {
  ensurePluginWatch();
  if (force) cached = null;
  if (!cached) {
    const p: Promise<ComponentCatalogEntry[]> = request<ComponentCatalogEntry[]>(CATALOG_PATH).catch((err) => {
      // Only clear OUR entry: a forced re-read (or a plugin reload) may have replaced the cache
      // while this request was in flight, and a stale rejection must not evict the newer one.
      if (cached === p) cached = null;
      throw err;
    });
    cached = p;
  }
  return cached;
}

/** Drop the cached catalog (the next fetch re-reads) and notify live consumers to re-fetch. */
export function refreshComponentCatalog(): void {
  cached = null;
  for (const fn of listeners) fn();
}

/** Subscribe to catalog invalidations (a plugin reload, or an explicit refresh). Returns an
 *  unsubscribe. Exported so a non-hook consumer can react to the same signal the hook does. */
export function onCatalogChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** The frame page a component `name` renders in, or null when it has none (a core widget, a
 *  frame-less plugin kind, or a name absent from the catalog). Pure — the resolution ORDER
 *  that picks a host for a name is S12b. */
export function frameUrlFor(name: string, catalog: readonly ComponentCatalogEntry[]): string | null {
  return catalog.find((entry) => entry.name === name)?.frame_url ?? null;
}

/** React view of the cached catalog: fetched on mount, re-fetched on every plugin (re)load.
 *  Returns the rows (empty until the first fetch resolves) plus a `frameUrl(name)` resolver. */
export function useComponentCatalog(): {
  catalog: ComponentCatalogEntry[];
  frameUrl: (name: string) => string | null;
} {
  const [catalog, setCatalog] = useState<ComponentCatalogEntry[]>([]);
  useEffect(() => {
    let alive = true;
    const load = () => {
      fetchComponentCatalog()
        .then((rows) => {
          if (alive) setCatalog(rows);
        })
        .catch(() => {
          if (alive) setCatalog([]);
        });
    };
    load();
    const off = onCatalogChange(load);
    return () => {
      alive = false;
      off();
    };
  }, []);
  return { catalog, frameUrl: (name) => frameUrlFor(name, catalog) };
}

// ── Host-mediated component reads (ADR 0118 D5 amendment, #4143) ─────────────────────────
// A frame component runs on an opaque origin with no bearer, so it asks its HOST to read one
// of its plugin's own GET routes. FrameComponentHost decides whether the read is allowed (the
// kind's declared `reads`, rate limits); this helper only performs the authed GET and shapes
// the result. The bearer goes into the request headers here and NOWHERE else — the result
// carries the status and the parsed JSON (or a reason), never headers or a raw body.

/** Largest response body a component read may return (bytes, UTF-8). */
export const COMPONENT_READ_MAX_BYTES = 256 * 1024;

/** What the host posts back to the frame (minus the `type`/`cid` envelope). `status` is the
 *  HTTP status, or 0 when the host refused or the request never produced a response. */
export type ComponentReadResult =
  | { ok: true; status: number; json: unknown }
  | { ok: false; status: number; error: string };

function isJsonContentType(ct: string | null): boolean {
  if (!ct) return false;
  const mime = ct.split(";")[0].trim().toLowerCase();
  return mime === "application/json" || mime.endsWith("+json");
}

/** GET `/api/plugins/<plugin>/<path>` with the operator's auth (slug-routed like every agent
 *  path) and return its JSON. `path` MUST already be validated against the kind's declared
 *  reads (FrameComponentHost does it) — this function trusts it. Refuses (ok:false) a redirect,
 *  a non-JSON content type, a body over {@link COMPONENT_READ_MAX_BYTES}, or unparseable JSON. A
 *  non-2xx answer is ok:false with the server's short detail. Never throws. */
export async function fetchComponentRead(
  plugin: string,
  path: string,
  opts: { signal?: AbortSignal; maxBytes?: number } = {},
): Promise<ComponentReadResult> {
  const maxBytes = opts.maxBytes ?? COMPONENT_READ_MAX_BYTES;
  let response: Response;
  try {
    response = await fetch(apiUrl(`/api/plugins/${encodeURIComponent(plugin)}/${path}`), {
      method: "GET",
      headers: applyAuth(new Headers({ Accept: "application/json" })),
      // A redirect could land outside the declared allowlist — refuse rather than follow.
      redirect: "error",
      signal: opts.signal,
    });
  } catch (err) {
    if ((err as { name?: string } | null)?.name === "AbortError") return { ok: false, status: 0, error: "read cancelled" };
    return { ok: false, status: 0, error: "read failed: the request did not complete" };
  }
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > maxBytes) {
    return { ok: false, status: response.status, error: `response too large (over ${maxBytes} bytes)` };
  }
  let raw: string;
  try {
    raw = await response.text();
  } catch {
    return { ok: false, status: response.status, error: "read failed: the response body could not be read" };
  }
  if (new TextEncoder().encode(raw).length > maxBytes) {
    return { ok: false, status: response.status, error: `response too large (over ${maxBytes} bytes)` };
  }
  if (!response.ok) {
    const { detail } = parseErrorBody(raw, `${response.status} ${response.statusText}`);
    return { ok: false, status: response.status, error: String(detail).slice(0, 200) || `HTTP ${response.status}` };
  }
  if (!isJsonContentType(response.headers.get("content-type"))) {
    return { ok: false, status: response.status, error: "response is not JSON" };
  }
  try {
    return { ok: true, status: response.status, json: JSON.parse(raw) as unknown };
  } catch {
    return { ok: false, status: response.status, error: "response is not valid JSON" };
  }
}
