// The live-sync change-feed cursor (ADR 0130 D3): the highest bus seq this origin has
// rendered for an agent, so the console's one EventSource resumes with `Last-Event-ID` /
// `?since=` after a reconnect, a tab regaining focus, an agent switch or a wake from sleep —
// and never replays or skips the durable change log.
//
// Two layers, so the transport (lib/events.ts) can read a cursor SYNCHRONOUSLY at connect
// time while it still survives a closed tab:
//
//   • sessionStorage — the synchronous, per-tab mirror. It is what `events.ts` reads the
//     instant it builds its first URL, and (through lib/storage.ts, the one storage seam)
//     the only cursor an origin whose IndexedDB is unavailable keeps.
//   • IndexedDB — the durable, per-ORIGIN store (chat/transcriptStore.ts, the one module
//     allowed to touch `indexedDB`). It outlives a closed tab, so a phone that slept or a
//     desktop pane reopened tomorrow resumes from its cursor instead of a blind full refetch.
//
// Reads merge both, newest wins; writes are monotonic (a cursor only moves forward) so a
// late or replayed frame never rewinds the resume point. Every path is best-effort: a
// missing store, a blocked write or a disabled IndexedDB degrades to the mirror alone.

import type { TranscriptStore } from "../chat/transcriptStore";
import { readKey, writeKey } from "./storage";

/** The sessionStorage key family (`storage.ts` KEY_REGISTRY `events.since`): the host
 *  window is un-suffixed, a slug agent is `:<slug>`. Keep this EXACTLY — the registry and
 *  the events-client tests pin it. */
export function cursorStorageKey(slug: string): string {
  return slug && slug !== "host" ? `protoagent.events.since:${slug}` : "protoagent.events.since";
}

/** The agent whose stream this tab follows, from the `/agent/<slug>` path, else the host
 *  window. Bus seqs are per-agent streams, so the cursor is keyed by it. */
export function agentSlug(): string {
  try {
    const m = globalThis.location?.pathname.match(/\/agent\/([^/?#]+)/);
    return m ? decodeURIComponent(m[1]) : "host";
  } catch {
    return "host";
  }
}

// slug → highest seq seen this page — the synchronous source of truth once hydrated.
const cache = new Map<string, number>();

// How the durable (IndexedDB) cursor is reached. Injected by the app at boot
// (`setSyncCursorStore`, main.tsx) so this module never imports the chat store — and so a
// unit test can hand it a fake-indexeddb-backed store. Null until registered, or whenever
// IndexedDB is unavailable (the console then keeps the sessionStorage mirror alone).
let storeFor: () => TranscriptStore | null = () => null;

export function setSyncCursorStore(provider: () => TranscriptStore | null): void {
  storeFor = provider;
}

function sessionSeq(slug: string): number | null {
  const raw = readKey("session", cursorStorageKey(slug));
  const n = raw === null ? NaN : Number(raw);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

/** The cursor to resume from, synchronously: the in-memory high-water, else the
 *  sessionStorage mirror, else null (a fresh tab starts live). The durable IndexedDB value
 *  folds in via `hydrateCursor`. */
export function loadCursor(slug: string): number | null {
  const cached = cache.get(slug);
  if (cached !== undefined) return cached;
  const stored = sessionSeq(slug);
  if (stored !== null) cache.set(slug, stored);
  return stored;
}

/** Record a rendered seq. Monotonic: only a higher seq moves the cursor, so a replayed or
 *  out-of-order frame can't rewind the resume point. Writes the sessionStorage mirror
 *  (synchronous) and the durable IndexedDB store (best-effort). Never throws. */
export function rememberCursor(slug: string, seq: number): void {
  if (!Number.isFinite(seq) || seq < 0) return;
  const current = loadCursor(slug);
  if (current !== null && seq <= current) return;
  cache.set(slug, seq);
  writeKey("session", cursorStorageKey(slug), String(seq)); // best-effort: live-only if it fails
  const store = storeFor();
  if (store) void store.putCursor(slug, seq).catch(() => {});
}

/** Fold the durable IndexedDB cursor into the cache, newest wins, and return the resolved
 *  value. Called once before the first connect (and cheap to re-call: the cache answers
 *  after the first read). Never throws — a missing/blocked store resolves to the mirror. */
export async function hydrateCursor(slug: string): Promise<number | null> {
  const store = storeFor();
  if (store) {
    let durable: number | null = null;
    try {
      durable = await store.loadCursor(slug);
    } catch {
      durable = null;
    }
    if (durable !== null) {
      const current = loadCursor(slug);
      if (current === null || durable > current) {
        cache.set(slug, durable);
        writeKey("session", cursorStorageKey(slug), String(durable));
      } else if (durable < current) {
        // The mirror is ahead of the durable store (this tab rendered past what was last
        // flushed) — carry the mirror back so a reopened tab resumes from the higher point.
        void store.putCursor(slug, current).catch(() => {});
      }
    }
  }
  return loadCursor(slug);
}

/** Test seam: forget the in-memory cache and the injected store. */
export function __resetSyncCursorForTests(): void {
  cache.clear();
  storeFor = () => null;
}
