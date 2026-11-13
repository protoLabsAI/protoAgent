// Agent-name cache (bd-aawcj). The boot splash in index.html names the agent it is loading before
// any bundle runs, by reading a display name the console cached for that slug. Writers go through
// the ADR 0114 storage seam here; the splash's inline script reads the SAME key raw (it runs before
// the seam is loaded), so the key format below and the one in index.html must stay in step.
//
// Keyed per agent slug (`protoagent.agentName:<slug>`, registered in lib/storage.ts). Written by
// the fleet switcher just before it navigates (so the next page can name the target) and by the
// boot gate once the focused agent's identity resolves (so a direct open / reload is seeded too).

import { readKey, writeKey } from "./storage";

const keyFor = (slug: string) => `protoagent.agentName:${slug}`;

/** Cache `name` as the display name for `slug`. Best-effort: a blank slug/name or a full/disabled
 *  store is a no-op — the splash just falls back to the generic "Opening agent…" copy. */
export function cacheAgentName(slug: string, name: string): void {
  if (!slug || !name) return;
  writeKey("local", keyFor(slug), name);
}

/** The cached display name for `slug`, or null if none (or storage is unavailable). */
export function cachedAgentName(slug: string): string | null {
  if (!slug) return null;
  const v = readKey("local", keyFor(slug));
  return v ? v : null;
}
