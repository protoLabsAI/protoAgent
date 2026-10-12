/**
 * The live component-v1 catalog (ADR 0118 D5 — `GET /api/components`, #4087/S11b): the core
 * widget kinds plus every plugin-contributed kind, each as `{name, plugin, frame_url}`. A
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
import { request } from "./http";

/** One row of `GET /api/components`: a renderable component-v1 kind. `plugin` is null for a
 *  core widget; `frame_url` is the public `/plugins/<id>/<frame>` page a frame kind renders
 *  in, else null (a core widget or a frame-less plugin kind). */
export type ComponentCatalogEntry = {
  name: string;
  plugin: string | null;
  frame_url: string | null;
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
 *  the cache. A failed fetch clears the cache so the next call retries, rather than handing
 *  back a stale rejected promise forever. */
export function fetchComponentCatalog(force = false): Promise<ComponentCatalogEntry[]> {
  ensurePluginWatch();
  if (force) cached = null;
  if (!cached) {
    cached = request<ComponentCatalogEntry[]>(CATALOG_PATH).catch((err) => {
      cached = null;
      throw err;
    });
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
