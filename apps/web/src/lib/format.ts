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

/** Byte counts — "512 B", "2.0 KB", "3.4 MB". */
export function bytes(n: number): string {
  if (n >= 1_048_576) return `${(n / 1_048_576).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}
