// Shared display formatters. Previously each surface carried its own near-identical
// `ago()` (activity / goals / playbooks / knowledge) and the telemetry panel held its
// own usd/tokens/ms/pct; consolidated here so the copy stays consistent.

/**
 * Relative time, e.g. "just now", "5m ago", "3h ago", "2d ago".
 *
 * Accepts an ISO-8601 string OR an epoch-**seconds** number (the goals panel's
 * shape). `null`/`undefined` → "never"; an unparseable value → "—".
 */
export function ago(input: string | number | null | undefined): string {
  if (input === null || input === undefined || input === "") return "never";
  const tMs = typeof input === "number" ? input * 1000 : Date.parse(input);
  if (Number.isNaN(tMs)) return "—";
  const s = Math.max(0, (Date.now() - tMs) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/** A thrown value coerced to a human-readable string (the `catch (e)` idiom, 40+ sites). */
export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * Compact LOCAL wall-clock stamp for an ISO instant — "08-10 11:45:45" in the
 * viewer's timezone. The old string-slice display preserved the source UTC
 * clock value while discarding its offset, so every stamp read four-plus hours
 * off for a non-UTC operator (#2468). An offsetless value is treated as UTC
 * (the telemetry store stamps UTC), never as local. Unparseable → "—".
 */
export function localStamp(iso: string | null | undefined): string {
  const tMs = parseInstant(iso);
  if (tMs === null) return "—";
  const d = new Date(tMs);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * Full local timestamp naming the timezone — for the tooltip/accessible text
 * behind `localStamp`, e.g. "2026-08-10, 11:45:45 EDT". Unparseable → the raw
 * input, so the title never loses information the cell had.
 */
export function localStampFull(iso: string | null | undefined): string {
  const tMs = parseInstant(iso);
  if (tMs === null) return iso || "—";
  return new Date(tMs).toLocaleString(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZoneName: "short",
  });
}

/**
 * Tooltip/accessible text behind `localStamp`: the full local timestamp AND the
 * raw source value — the raw ISO keeps microsecond precision and the original
 * offset, which the locale rendering drops (QA review on #2468). Falls back to
 * the raw input alone when unparseable.
 */
export function localStampTitle(iso: string | null | undefined): string {
  if (!iso) return "—";
  const full = localStampFull(iso);
  return full === iso ? iso : `${full} · ${iso}`;
}

/** Epoch ms for an ISO string, treating an offsetless value as UTC. null = unparseable. */
function parseInstant(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const hasOffset = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(iso);
  const tMs = Date.parse(hasOffset ? iso : `${iso}Z`);
  return Number.isNaN(tMs) ? null : tMs;
}

/** Money — "$0", "$0.0042" under a cent, else two decimals. */
export function usd(n: number): string {
  if (!n) return "$0";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

/** Compact token counts — "1.2M", "3.4k", or the raw number. */
export function tokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** Latency — "—" for zero, "1.2s" at/over a second, else "850ms". */
export function ms(n: number): string {
  if (!n) return "—";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${n}ms`;
}

/** A 0–1 ratio as a whole-number percentage. */
export function pct(n: number): string {
  return `${Math.round((n || 0) * 100)}%`;
}

/** Byte counts — "512 B", "2.0 KB", "3.4 MB", "1.2 GB"; ≥10 in a unit drops the decimal ("14 KB"). */
export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}
