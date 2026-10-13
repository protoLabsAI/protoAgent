// The operator's openLink origin allowlist — `artifact.open_link_origins` (Settings ▸ Plugins ▸
// Artifact, ADR 0118 D4). Empty = any https origin; a non-empty list narrows openLink() to exactly
// those origins. Read from the live config when a frame asks to open a link (a rare, rate-limited
// event), with a short cache so a burst of proposals costs one request and a Settings save still
// applies within seconds. A failed read REJECTS (the caller refuses the link): an operator who
// narrowed the list must never have it silently widened by a network blip.

import { api } from "../lib/api";

const CACHE_MS = 5_000;
let cache: { at: number; origins: readonly string[] } | null = null;

/** The allowlist as configured: every non-empty string entry; `[]` when unset. */
export function openLinkOriginsFromConfig(config: unknown): readonly string[] {
  const section = (config as { artifact?: { open_link_origins?: unknown } } | null)?.artifact;
  const raw = section?.open_link_origins;
  if (!Array.isArray(raw)) return [];
  return raw.filter((o): o is string => typeof o === "string" && o.trim() !== "");
}

export async function loadOpenLinkOrigins(now: () => number = Date.now): Promise<readonly string[]> {
  const t = now();
  if (cache && t - cache.at < CACHE_MS) return cache.origins;
  const payload = await api.config();
  const origins = openLinkOriginsFromConfig(payload?.config);
  cache = { at: t, origins };
  return origins;
}

/** Test seam: forget the cached allowlist. */
export function resetOpenLinkOriginsCacheForTest(): void {
  cache = null;
}
