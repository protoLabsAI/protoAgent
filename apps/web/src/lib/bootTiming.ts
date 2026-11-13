// Boot timing marks (bd-aawcj). An agent switch is a full page load, and in the desktop webview
// the bundled assets (2.1 MB entry JS + 357 KB CSS) are re-read on every switch — so the cost that
// remains after the splash + font fix needs to be measurable in the field. These three marks span
// the boot path; `logBootTimings` emits the segment durations once at debug level when the app is
// ready, and also records `performance.measure` spans so a devtools Performance capture in the
// desktop shows them on the timeline.

const PREFIX = "pa:boot:";

export type BootMark = "script-start" | "first-render" | "ready";

/** Mark a boot milestone. Idempotent per name (first call wins), so marking `first-render`
 *  from App's render body stays a single point even across re-renders / StrictMode. Never throws. */
export function markBoot(name: BootMark): void {
  try {
    if (performance.getEntriesByName(PREFIX + name).length > 0) return;
    performance.mark(PREFIX + name);
  } catch {
    // performance API unavailable (very old/embedded webview) — timings are best-effort.
  }
}

let logged = false;

function markTime(name: BootMark): number | null {
  const entries = performance.getEntriesByName(PREFIX + name);
  return entries.length ? entries[entries.length - 1].startTime : null;
}

/** Log script-start→first-render, first-render→ready and script-start→ready once, at debug level.
 *  A no-op if any mark is missing or the performance API is unavailable. */
export function logBootTimings(): void {
  if (logged) return;
  logged = true;
  try {
    const start = markTime("script-start");
    const first = markTime("first-render");
    const ready = markTime("ready");
    if (start == null || first == null || ready == null) return;
    try {
      performance.measure(`${PREFIX}script-start→first-render`, PREFIX + "script-start", PREFIX + "first-render");
      performance.measure(`${PREFIX}first-render→ready`, PREFIX + "first-render", PREFIX + "ready");
      performance.measure(`${PREFIX}script-start→ready`, PREFIX + "script-start", PREFIX + "ready");
    } catch {
      // measure() can throw if a mark went missing between the reads above — the debug log stands.
    }
    const round = (ms: number) => Math.round(ms);
    console.debug("[boot] timings (ms)", {
      "script-start→first-render": round(first - start),
      "first-render→ready": round(ready - first),
      "script-start→ready": round(ready - start),
    });
  } catch {
    // Never let timing instrumentation affect boot.
  }
}

/** Test-only: allow `logBootTimings` to run again. */
export function __resetBootTimingForTests(): void {
  logged = false;
}
