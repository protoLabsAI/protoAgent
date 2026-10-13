// The host-side gates for a frame component's READ channel (ADR 0118 D5 amendment, #4143).
//
// A frame-rendered plugin component runs on an opaque origin with no bearer. It may ask its
// host to read one of its plugin's OWN GET routes by posting `protoComponent:read {cid, path}`,
// where `path` is relative to `/api/plugins/<plugin>/`. FrameComponentHost answers with
// `protoComponent:readResult {cid, ok, status, json | error}`. Every decision here is the
// host's — nothing about the request is trusted from the frame:
//   • the path must match one of the kind's DECLARED `reads` (from GET /api/components, which the
//     server validated at registration): same segment count, each segment equal or a `*`;
//   • GET only — a request naming any other method is refused;
//   • at most READ_RATE_PER_SEC reads per rolling second and READ_MAX_IN_FLIGHT in flight, per
//     frame occurrence;
//   • the response must be JSON of at most 256 KB (fetchComponentRead in lib/api/components.ts).
//
// Pure functions + a tiny limiter, so each refusal is unit-testable without a frame.

/** Reads a single frame may START per rolling second. */
export const READ_RATE_PER_SEC = 4;
/** Reads a single frame may have outstanding at once. */
export const READ_MAX_IN_FLIGHT = 2;
/** Longest `path` the host will consider. */
export const READ_MAX_PATH_LENGTH = 512;

// One concrete path segment: URL-safe characters only (mirrors the server's pattern charset), so
// a path can't carry a query (`?`), a fragment (`#`), a percent-encoded `..`, or a backslash.
const SEGMENT_RE = /^[A-Za-z0-9._~:@+=,-]+$/;

/** The validated segments of a frame-supplied `path`, or null when it is malformed: not a
 *  string, blank, too long, absolute, or any segment empty / `.` / `..` / non-URL-safe. */
export function parseReadPath(path: unknown): string[] | null {
  if (typeof path !== "string" || !path || path.length > READ_MAX_PATH_LENGTH) return null;
  if (path.startsWith("/")) return null; // relative to the plugin prefix — never absolute
  const segs = path.split("/");
  for (const seg of segs) {
    if (seg === "." || seg === ".." || !SEGMENT_RE.test(seg)) return null;
  }
  return segs;
}

/** True when the path segments match `pattern` exactly: the same number of segments, each one
 *  equal to the pattern's or matched by a whole-segment `*` (one segment, never more). */
export function matchesReadPattern(segs: readonly string[], pattern: string): boolean {
  const want = pattern.split("/");
  if (want.length !== segs.length) return false;
  return want.every((w, i) => w === "*" || w === segs[i]);
}

export type ReadCheck = { ok: true; path: string } | { ok: false; error: string };

/** Decide whether a frame's read request may proceed — every check that doesn't need state
 *  (the limiter is separate). `plugin` is the kind's owning plugin from the catalog; `reads` its
 *  declared patterns. */
export function checkReadRequest(
  msg: { path?: unknown; method?: unknown },
  plugin: string | null | undefined,
  reads: readonly string[] | undefined,
): ReadCheck {
  if (msg.method !== undefined && (typeof msg.method !== "string" || msg.method.toUpperCase() !== "GET")) {
    return { ok: false, error: "reads are GET only" };
  }
  if (!plugin) return { ok: false, error: "this component has no plugin to read from" };
  if (!reads || reads.length === 0) return { ok: false, error: "this component declares no reads" };
  const segs = parseReadPath(msg.path);
  if (!segs) return { ok: false, error: "invalid read path" };
  if (!reads.some((p) => matchesReadPattern(segs, p))) {
    return { ok: false, error: "path is not in this component's declared reads" };
  }
  return { ok: true, path: segs.join("/") };
}

/** Per-frame read limiter: a rolling one-second start window plus an in-flight cap. */
export type ReadLimiter = {
  /** Claim a slot at `now` (ms). Returns null when granted (call `release` when the read
   *  settles), or the refusal reason. */
  acquire(now: number): string | null;
  release(): void;
  inFlight(): number;
};

export function createReadLimiter(perSec = READ_RATE_PER_SEC, maxInFlight = READ_MAX_IN_FLIGHT): ReadLimiter {
  let starts: number[] = [];
  let inFlight = 0;
  return {
    acquire(now) {
      starts = starts.filter((t) => now - t < 1000);
      if (inFlight >= maxInFlight) return `too many reads in flight (max ${maxInFlight})`;
      if (starts.length >= perSec) return `read rate limited (max ${perSec} per second)`;
      starts.push(now);
      inFlight += 1;
      return null;
    },
    release() {
      inFlight = Math.max(0, inFlight - 1);
    },
    inFlight: () => inFlight,
  };
}
