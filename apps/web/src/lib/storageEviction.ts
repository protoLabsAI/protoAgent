// Browser-storage eviction (ADR 0114 D4/D5, the localStorage steps that come before the IDB move).
//
// One origin holds every agent's transcripts — the desktop app and every fleet member opened
// through the hub share it — and nothing ever removed an old agent's history, so the ~5 MB
// localStorage quota filled up with chats nobody would open again (3.37 MB on 2026-11-09, 700 KB
// of it agents no longer in the fleet; #4972). Once full, saves fail and history stops saving.
//
// Eviction removes WHOLE transcript keys of agents that are not live, in this order:
//   1. agents no longer in the fleet, least recently written first;
//   2. fleet agents not written in the last 24h, least recently written first. A chat the
//      server still holds comes back from its 24h window (ADR 0104).
// Never evicted: this window's agent, the host, any agent written in the last 24h, and
// every non-transcript key (auth, tenant, theme, prefs, dismissal sets, layout).
//
// It runs from two places: storage.ts's quota hook (a write just failed, so free at least what
// it needs), and once a session, `enforceStorageBudget`, a little ahead of the error (the
// 4 MB budget is a heuristic; the quota error is authoritative).

import { currentSlug } from "./api/routing";
import {
  entryBytes,
  listKeys,
  matchKey,
  readKey,
  registerEvictionHook,
  removeKey,
  usageBytes,
} from "./storage";

/** UTF-16 bytes of localStorage the console aims to stay under (WebKit/Chromium allow ~5 MB). */
export const LOCAL_BUDGET_BYTES = 4 * 1024 * 1024;
/** After a budget eviction, stop once usage is back under this — room to grow before the next one. */
export const LOCAL_TARGET_BYTES = Math.floor(LOCAL_BUDGET_BYTES * 0.85);
/** A transcript written this recently is treated as live: an open (maybe frozen) window owns it. */
export const RECENT_WRITE_MS = 24 * 60 * 60 * 1000;

export type Evicted = { key: string; slug: string; bytes: number };
export type EvictionReport = { reason: "quota" | "budget"; evicted: Evicted[]; freedBytes: number };

let fleetSlugs: ReadonlySet<string> | null = null;

/** The current backend's fleet, by slug — only from a full fleet read (one that includes the
 *  host entry). `null` = unknown: then no agent counts as "no longer in the fleet". */
export function setFleetSlugs(slugs: Iterable<string> | null): void {
  fleetSlugs = slugs ? new Set(slugs) : null;
}

/** When the transcript key was last written, from its own content; `null` = unknown. A chat
 *  blob carries `sessions[].updatedAt`; palette/DM threads carry no time at all. */
export function lastWrittenAt(value: string | null): number | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as { sessions?: { updatedAt?: unknown }[] };
    if (!Array.isArray(parsed?.sessions)) return null;
    let newest: number | null = null;
    for (const session of parsed.sessions) {
      const at = typeof session?.updatedAt === "number" ? session.updatedAt : null;
      if (at !== null && (newest === null || at > newest)) newest = at;
    }
    return newest;
  } catch {
    return null;
  }
}

type Candidate = Evicted & { tier: 1 | 2; writtenAt: number; /** chunk keys removed with it */ also: string[] };

/** Transcript keys eviction may remove, in eviction order. `exclude` is the key a failing
 *  write is trying to save, which is never removed out from under it. */
export function evictionCandidates(opts: { now?: number; exclude?: string } = {}): Candidate[] {
  const now = opts.now ?? Date.now();
  const here = currentSlug();
  const out: Candidate[] = [];
  const keys = listKeys("local");
  const present = new Set(keys);
  // A chat's chunks (#4973) go with their agent's main key; counted into its size here.
  const chunksOf = new Map<string, string[]>();
  for (const key of keys) {
    if (matchKey("local", key)?.spec.id !== "chat.chunk") continue;
    const main = key.slice(0, key.indexOf(".chunk."));
    chunksOf.set(main, [...(chunksOf.get(main) ?? []), key]);
  }
  for (const key of keys) {
    if (key === opts.exclude) continue;
    const match = matchKey("local", key);
    if (!match || match.spec.category !== "transcript" || !match.spec.evictable) continue;
    const slug = match.slug ?? "host";
    if (match.spec.id === "chat.chunk") {
      // Only an ORPHAN chunk (its main key is gone) stands alone; nothing reads it.
      const main = key.slice(0, key.indexOf(".chunk."));
      if (present.has(main) || main === opts.exclude) continue;
      out.push({ key, slug, bytes: entryBytes(key, readKey("local", key) ?? ""), tier: 1, writtenAt: 0, also: [] });
      continue;
    }
    if (slug === "host" || slug === here) continue;
    const value = readKey("local", key);
    const written = lastWrittenAt(value);
    if (written !== null && now - written < RECENT_WRITE_MS) continue;
    const inFleet = fleetSlugs === null ? true : fleetSlugs.has(slug);
    // A fleet agent's key is only evicted when we KNOW it's been quiet; a thread with no
    // time (palette/DM) might be in use, so it goes only once its agent has left the fleet.
    if (inFleet && written === null) continue;
    const also = chunksOf.get(key) ?? [];
    const bytes = also.reduce((n, chunk) => n + entryBytes(chunk, readKey("local", chunk) ?? ""), entryBytes(key, value ?? ""));
    out.push({ key, slug, bytes, tier: inFleet ? 2 : 1, writtenAt: written ?? 0, also });
  }
  return out.sort((a, b) => a.tier - b.tier || a.writtenAt - b.writtenAt || b.bytes - a.bytes);
}

const listeners = new Set<(report: EvictionReport) => void>();

/** Hear about every eviction that removed something (the "freed storage" toast). */
export function subscribeEvictions(fn: (report: EvictionReport) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Remove candidates in order until `needBytes` are freed (or none are left). */
export function evict(needBytes: number, opts: { reason: EvictionReport["reason"]; now?: number; exclude?: string }): EvictionReport {
  const evicted: Evicted[] = [];
  let freedBytes = 0;
  for (const candidate of evictionCandidates(opts)) {
    if (freedBytes >= needBytes) break;
    removeKey("local", candidate.key);
    for (const chunk of candidate.also) removeKey("local", chunk);
    evicted.push({ key: candidate.key, slug: candidate.slug, bytes: candidate.bytes });
    freedBytes += candidate.bytes;
  }
  const report: EvictionReport = { reason: opts.reason, evicted, freedBytes };
  if (evicted.length) {
    console.info("[storage] evicted old transcripts", { reason: opts.reason, freedBytes, keys: evicted.map((e) => e.key) });
    for (const fn of listeners) {
      try {
        fn(report);
      } catch {
        // A listener must never break eviction (or the write that triggered it).
      }
    }
  }
  return report;
}

/** Over the budget? Evict down to LOCAL_TARGET_BYTES. Returns what it did (nothing when under). */
export function enforceStorageBudget(opts: { now?: number; usage?: number } = {}): EvictionReport {
  const usage = opts.usage ?? usageBytes("local");
  if (usage <= LOCAL_BUDGET_BYTES) return { reason: "budget", evicted: [], freedBytes: 0 };
  return evict(usage - LOCAL_TARGET_BYTES, { reason: "budget", now: opts.now });
}

/** Wire eviction into storage.ts's quota path: a failing write frees at least its own size. */
export function installStorageEviction(): () => void {
  registerEvictionHook(({ key, bytes }) => evict(bytes, { reason: "quota", exclude: key }).freedBytes);
  return () => registerEvictionHook(null);
}
