// The console's ONE browser-storage seam (ADR 0114 D1).
//
// Every `localStorage` / `sessionStorage` access in the console goes through here — a vitest
// source guard (lib/storageGuard.test.ts) fails the build on any other call site. The rule
// this module exists to enforce: browser storage is a best-effort cache, so NO render and NO
// subscription may depend on a write succeeding. A full quota used to throw straight out of
// zustand's `persist` (it calls `setItem` synchronously inside `set()` and never catches)
// into the root error boundary, and Reload looped back to the crash.
//
// - `readKey` / `writeKey` / `removeKey` never throw. `writeKey` reports why it failed.
// - `writeKeyStrict` throws — for credentials only, where "saved" must mean saved.
// - `persistStorage(area)` is the zustand `StateStorage` adapter: `setItem` never throws.
// - The KEY REGISTRY names every key the console writes, by EXACT pattern (never a prefix:
//   `protoagent.chat.sessions:<slug>.dismissed` is a dismissal set, not a transcript).
// - The QUOTA LATCH: on a localStorage quota error run the eviction hook once, retry once,
//   then latch `failing`; while latched, writes to evictable categories fail fast.
//
// Deliberately import-free: AppCrash (the root boundary's fallback) imports this, and it
// must stay dependency-light because anything heavier may be the thing that threw.

export type StorageArea = "local" | "session";

export type KeyCategory =
  | "auth"
  | "tenant"
  | "theme"
  | "layout"
  | "prefs"
  | "transcript"
  | "index"
  | "dismissals"
  | "ephemeral";

export type WriteResult = { ok: true } | { ok: false; reason: "quota" | "unavailable" };

export type KeySpec = {
  /** Stable id for code that needs one specific key family (e.g. "chat.sessions"). */
  id: string;
  area: StorageArea;
  /** Anchored, exact pattern. Never a bare prefix. */
  pattern: RegExp;
  category: KeyCategory;
  /** The agent slug the key belongs to ("host" for the un-suffixed key), or null when the
   *  key is not per-agent. Always derived here — callers never parse suffixes (ADR 0114 D4). */
  slug: (m: RegExpMatchArray) => string | null;
  /** May eviction (S3) remove it under pressure? Also: writes to an evictable key fail fast
   *  while the quota latch is set. */
  evictable: boolean;
};

const hostOr = (s: string | undefined) => (s ? s : "host");
const noSlug = () => null;

// Slugs never contain ":" or "/" (URL path segment, ADR 0042). The class also refuses ".",
// so a `.dismissed` suffix can never be read as part of a slug; a dotted slug is simply
// unregistered (counted, never touched) rather than misfiled.
const SLUG = "([^:/.]+)";

export const KEY_REGISTRY: readonly KeySpec[] = [
  // ── credentials / connection ──
  { id: "authToken", area: "local", pattern: /^protoagent\.authToken$/, category: "auth", slug: noSlug, evictable: false },
  { id: "deviceId", area: "local", pattern: /^protoagent\.deviceId$/, category: "auth", slug: noSlug, evictable: false },
  { id: "apiBase", area: "local", pattern: /^protoagent\.apiBase$/, category: "auth", slug: noSlug, evictable: false },
  // ── tenant ──
  { id: "tenant.uid", area: "local", pattern: /^protoagent\.tenant\.uid$/, category: "tenant", slug: noSlug, evictable: false },
  { id: "tenant.switched", area: "session", pattern: /^protoagent\.tenant\.switched$/, category: "ephemeral", slug: noSlug, evictable: false },
  // ── theme (the DS's own key + our owner stamp) ──
  { id: "pl-theme", area: "local", pattern: /^pl-theme$/, category: "theme", slug: noSlug, evictable: false },
  { id: "pl-theme.owner", area: "local", pattern: /^pl-theme:agent$/, category: "theme", slug: noSlug, evictable: false },
  // ── layout (ADR 0035 D5, per agent ADR 0042) ──
  { id: "ui", area: "local", pattern: new RegExp(`^protoagent\\.ui(?::${SLUG})?$`), category: "layout", slug: (m) => hostOr(m[1]), evictable: true },
  { id: "uislice", area: "local", pattern: /^proto:uislice:([^:]+)(?::([^:/]+))?$/, category: "layout", slug: (m) => hostOr(m[2]), evictable: true },
  // ── transcripts ──
  { id: "chat.sessions", area: "local", pattern: new RegExp(`^protoagent\\.chat\\.sessions(?::${SLUG})?$`), category: "transcript", slug: (m) => hostOr(m[1]), evictable: true },
  {
    id: "palette.chat",
    area: "local",
    // protoagent.palette.chat[:<slug>][:dm:<member>] — the slug group refuses a literal "dm"
    // so the host window's DM thread (`protoagent.palette.chat:dm:<member>`) parses right.
    pattern: /^protoagent\.palette\.chat(?::(?!dm:)([^:/]+))?(?::dm:([^:/]+))?$/,
    category: "transcript",
    slug: (m) => hostOr(m[1]),
    evictable: true,
  },
  // ── dismissal / seen sets ──
  { id: "chat.dismissed", area: "local", pattern: new RegExp(`^protoagent\\.chat\\.sessions(?::${SLUG})?\\.dismissed$`), category: "dismissals", slug: (m) => hostOr(m[1]), evictable: false },
  { id: "chat.dismissedReports", area: "local", pattern: /^protoagent\.chat\.dismissedReports$/, category: "dismissals", slug: noSlug, evictable: false },
  { id: "chat.dismissedScheduled", area: "local", pattern: /^protoagent\.chat\.dismissedScheduled$/, category: "dismissals", slug: noSlug, evictable: false },
  { id: "chat.dismissedToolCalls", area: "local", pattern: /^protoagent\.chat\.dismissedToolCalls$/, category: "dismissals", slug: noSlug, evictable: false },
  { id: "bgjobs.seen", area: "local", pattern: /^protoagent\.bgjobs\.seen$/, category: "dismissals", slug: noSlug, evictable: false },
  // ── prefs ──
  { id: "keybindings", area: "local", pattern: /^protoagent\.keybindings$/, category: "prefs", slug: noSlug, evictable: false },
  { id: "chat.inputHistory", area: "local", pattern: /^protoagent\.chat\.inputHistory$/, category: "prefs", slug: noSlug, evictable: false },
  { id: "fleet.recent", area: "local", pattern: /^protoagent\.fleet\.recent$/, category: "prefs", slug: noSlug, evictable: false },
  { id: "palette.recent", area: "local", pattern: /^protoagent\.palette\.recent$/, category: "prefs", slug: noSlug, evictable: false },
  { id: "codePane.widened", area: "local", pattern: /^protoagent\.codePane\.widened$/, category: "prefs", slug: noSlug, evictable: false },
  { id: "kb.openGroups", area: "local", pattern: /^protoagent\.kb\.openGroups$/, category: "prefs", slug: noSlug, evictable: false },
  { id: "editor", area: "local", pattern: /^protoagent\.editor$/, category: "prefs", slug: noSlug, evictable: false },
  { id: "openFilesIn", area: "local", pattern: /^protoagent\.openFilesIn$/, category: "prefs", slug: noSlug, evictable: false },
  { id: "projectPath", area: "local", pattern: /^protoagent\.projectPath$/, category: "prefs", slug: noSlug, evictable: false },
  // ── sessionStorage (per tab) ──
  { id: "schedwatch.notified", area: "session", pattern: /^protoagent\.schedwatch\.notified$/, category: "ephemeral", slug: noSlug, evictable: false },
  { id: "bgwatch.notified", area: "session", pattern: /^protoagent\.bgwatch\.notified$/, category: "ephemeral", slug: noSlug, evictable: false },
  { id: "turnwatch.notified", area: "session", pattern: /^protoagent\.turnwatch\.notified$/, category: "ephemeral", slug: noSlug, evictable: false },
  { id: "setupGapDismissals", area: "session", pattern: /^protoagent\.setupGapDismissals:(.+)$/, category: "ephemeral", slug: (m) => m[1], evictable: false },
  { id: "chat.scratch", area: "session", pattern: /^protoagent\.chat\.(draft|steers|scroll):([^:]+):(.+)$/, category: "ephemeral", slug: (m) => m[2], evictable: false },
  { id: "codePane", area: "session", pattern: /^protoagent\.codePane$/, category: "ephemeral", slug: noSlug, evictable: false },
  { id: "events.since", area: "session", pattern: new RegExp(`^protoagent\\.events\\.since(?::${SLUG})?$`), category: "ephemeral", slug: (m) => hostOr(m[1]), evictable: false },
];

export type KeyMatch = { spec: KeySpec; slug: string | null };

/** The registry entry a key belongs to, by exact pattern — or null for an unregistered key
 *  (plugins, the design system), which is counted but never touched. */
export function matchKey(area: StorageArea, key: string): KeyMatch | null {
  for (const spec of KEY_REGISTRY) {
    if (spec.area !== area) continue;
    const m = key.match(spec.pattern);
    if (m) return { spec, slug: spec.slug(m) };
  }
  return null;
}

// ── quota detection ────────────────────────────────────────────────────────────────────

/** Chromium: name QuotaExceededError / code 22. Firefox: NS_ERROR_DOM_QUOTA_REACHED / 1014.
 *  WebKit: QuotaExceededError ("The quota has been exceeded."). */
export function isQuotaError(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const e = err as { name?: unknown; code?: unknown };
  return (
    e.name === "QuotaExceededError" ||
    e.name === "NS_ERROR_DOM_QUOTA_REACHED" ||
    e.code === 22 ||
    e.code === 1014
  );
}

function syntheticQuotaError(): Error {
  try {
    return new DOMException("The quota has been exceeded.", "QuotaExceededError");
  } catch {
    const e = new Error("The quota has been exceeded.");
    e.name = "QuotaExceededError";
    return e;
  }
}

// ── raw access ─────────────────────────────────────────────────────────────────────────

function store(area: StorageArea): Storage | null {
  try {
    const s = area === "local" ? globalThis.localStorage : globalThis.sessionStorage;
    return s && typeof s.getItem === "function" ? s : null;
  } catch {
    return null; // SecurityError: storage disabled for this origin / sandboxed frame
  }
}

/** UTF-16 bytes a key/value pair costs against the quota. */
export function entryBytes(key: string, value: string): number {
  return 2 * (key.length + value.length);
}

/** Every key currently in `area` (a snapshot — safe to remove while iterating it). */
export function listKeys(area: StorageArea): string[] {
  const s = store(area);
  if (!s) return [];
  const out: string[] = [];
  try {
    for (let i = 0; i < s.length; i++) {
      const k = s.key(i);
      if (k !== null) out.push(k);
    }
  } catch {
    /* unavailable mid-scan — what we have */
  }
  return out;
}

/** Every key with its size, largest first — registered or not. */
export function keySizes(area: StorageArea = "local"): { key: string; bytes: number }[] {
  return listKeys(area)
    .map((key) => ({ key, bytes: entryBytes(key, readKey(area, key) ?? "") }))
    .sort((a, b) => b.bytes - a.bytes);
}

/** Total UTF-16 bytes used in `area`, counting every key. */
export function usageBytes(area: StorageArea = "local"): number {
  return keySizes(area).reduce((n, e) => n + e.bytes, 0);
}

// ── dev flag: storage.simulateQuotaBytes (ADR 0068) ────────────────────────────────────
// `?flag:storage.simulateQuotaBytes=<N>` on the console URL (or the test hook
// `globalThis.__protoagentSimulateQuotaBytes = N`) makes localStorage writes throw a synthetic
// QuotaExceededError once total usage would exceed N bytes — a full quota on demand, for QA
// and e2e. Read here rather than through flags/flags.ts: a numeric knob, and this module must
// stay import-free.

type SimGlobals = { __protoagentSimulateQuotaBytes?: number };

const _querySimBytes: number | null = (() => {
  try {
    const raw = new URLSearchParams(globalThis.location?.search ?? "").get("flag:storage.simulateQuotaBytes");
    const n = raw === null ? NaN : Number(raw);
    return Number.isFinite(n) && n >= 0 ? n : null;
  } catch {
    return null;
  }
})();

function simulatedQuota(): number | null {
  const g = (globalThis as SimGlobals).__protoagentSimulateQuotaBytes;
  if (typeof g === "number" && Number.isFinite(g) && g >= 0) return g;
  return _querySimBytes;
}

function rawSet(area: StorageArea, s: Storage, key: string, value: string): void {
  const cap = area === "local" ? simulatedQuota() : null;
  if (cap !== null) {
    const prev = s.getItem(key);
    const next = usageBytes("local") - (prev === null ? 0 : entryBytes(key, prev)) + entryBytes(key, value);
    if (next > cap) throw syntheticQuotaError();
  }
  s.setItem(key, value);
}

// ── pressure state + quota latch ───────────────────────────────────────────────────────

export type StoragePressure = { state: "ok" | "failing" };

let _pressure: StoragePressure = { state: "ok" };
const _pressureListeners = new Set<() => void>();

function setPressure(state: StoragePressure["state"]) {
  if (_pressure.state === state) return;
  _pressure = { state };
  _pressureListeners.forEach((fn) => {
    try {
      fn();
    } catch (err) {
      console.error("[storage] pressure listener threw", err);
    }
  });
}

/** Current storage pressure — `failing` while the localStorage quota latch is set. */
export function storagePressure(): StoragePressure {
  return _pressure;
}

/** Subscribe to pressure transitions (useSyncExternalStore-compatible). */
export function subscribeStoragePressure(fn: () => void): () => void {
  _pressureListeners.add(fn);
  return () => {
    _pressureListeners.delete(fn);
  };
}

/** The eviction hook runs once per localStorage quota error, before the single retry. It
 *  returns the bytes it freed; anything > 0 releases the latch. Slice 1 registers none —
 *  eviction (ADR 0114 D5) arrives in slice 3. */
export type EvictionHook = (ctx: { key: string; bytes: number }) => number;

let _evict: EvictionHook = () => 0;

export function registerEvictionHook(hook: EvictionHook | null): void {
  _evict = hook ?? (() => 0);
}

function releaseLatch() {
  setPressure("ok");
}

function runEviction(key: string, bytes: number): number {
  try {
    const freed = _evict({ key, bytes });
    if (freed > 0) releaseLatch();
    return freed;
  } catch (err) {
    console.error("[storage] eviction hook threw", err);
    return 0;
  }
}

try {
  // Another tab removed or rewrote something — space may have come back.
  globalThis.addEventListener?.("storage", () => releaseLatch());
} catch {
  /* non-browser context */
}

// ── public API ─────────────────────────────────────────────────────────────────────────

/** Read a key. Never throws: missing, unavailable, or blocked all read as null. */
export function readKey(area: StorageArea, key: string): string | null {
  const s = store(area);
  if (!s) return null;
  try {
    const v = s.getItem(key);
    return typeof v === "string" ? v : null;
  } catch {
    return null;
  }
}

/** Write a key. Never throws. On a localStorage quota error: evict once, retry once, then
 *  latch `failing`. While latched, evictable keys fail fast without touching storage. */
export function writeKey(area: StorageArea, key: string, value: string): WriteResult {
  const s = store(area);
  if (!s) return { ok: false, reason: "unavailable" };
  const evictable = matchKey(area, key)?.spec.evictable ?? false;
  if (area === "local" && evictable && _pressure.state === "failing") return { ok: false, reason: "quota" };
  try {
    rawSet(area, s, key, value);
    return { ok: true };
  } catch (err) {
    if (!isQuotaError(err)) return { ok: false, reason: "unavailable" };
    // sessionStorage has its own per-tab quota: no eviction, no latch — just report it.
    if (area === "session") return { ok: false, reason: "quota" };
  }
  runEviction(key, entryBytes(key, value));
  try {
    rawSet(area, s, key, value);
    return { ok: true };
  } catch (err) {
    if (!isQuotaError(err)) return { ok: false, reason: "unavailable" };
    setPressure("failing");
    return { ok: false, reason: "quota" };
  }
}

export class StorageWriteError extends Error {
  readonly reason: "quota" | "unavailable";
  constructor(key: string, reason: "quota" | "unavailable") {
    super(
      reason === "quota"
        ? `Browser storage is full — could not save ${key}`
        : `Browser storage is unavailable — could not save ${key}`,
    );
    this.name = "StorageWriteError";
    this.reason = reason;
  }
}

/** `writeKey` that THROWS on failure — for credentials, where the caller must not report
 *  success after the browser failed to keep the value (ADR 0114 D1). */
export function writeKeyStrict(area: StorageArea, key: string, value: string): void {
  const res = writeKey(area, key, value);
  if (!res.ok) throw new StorageWriteError(key, res.reason);
}

/** Remove a key. Never throws. A localStorage removal releases the quota latch. */
export function removeKey(area: StorageArea, key: string): void {
  const s = store(area);
  if (!s) return;
  try {
    s.removeItem(key);
  } catch {
    return;
  }
  if (area === "local") releaseLatch();
}

/** The zustand `StateStorage` adapter (for `createJSONStorage(() => persistStorage(area))`).
 *  `getItem` returns exactly `string | null`; `setItem` never throws — including on zustand's
 *  migrate-on-hydrate path, where `setItem` runs inside hydration. `mapKey` turns the store's
 *  `name` into the real key (the per-agent suffix). A write whose value already equals what
 *  storage holds is skipped, so no-op sets never rewrite the blob (or retry a full quota). */
export function persistStorage(area: StorageArea, mapKey: (name: string) => string = (n) => n) {
  return {
    getItem: (name: string): string | null => readKey(area, mapKey(name)),
    setItem: (name: string, value: string): void => {
      const key = mapKey(name);
      if (readKey(area, key) === value) return;
      writeKey(area, key, value);
    },
    removeItem: (name: string): void => removeKey(area, mapKey(name)),
  };
}

/** Test-only: reset the latch + hook between cases. */
export function __resetStorageSeamForTests(): void {
  _pressure = { state: "ok" };
  _evict = () => 0;
}
