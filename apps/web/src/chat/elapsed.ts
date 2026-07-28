// Live elapsed time for a RUNNING tool card.
//
// Why this exists: a running card showed a spinner and nothing else. `durationMs` is only
// computed at the tool-END frame (ChatSurface.tsx), so while a call was in flight the DS
// ToolCard had no duration to render. A card three seconds in and one fifteen minutes in
// looked exactly the same, and "how long has this been going?" is the question behind
// "should I stop it?". Everything needed is already on the wire — `startedAt` is stamped
// when the tool-start frame arrives.
//
// What this is NOT: evidence the call is alive. The value is `now - startedAt`, computed
// in the browser, so it climbs identically whether the tool is working or wedged. No
// client-side clock can tell those apart — it would take a server-observed progress
// signal. Ending a wedged turn is the server's job (the stall watchdog, #2349); this
// just makes its AGE visible while it runs. Keep the copy honest about that.

import { useEffect, useState } from "react";

/** Don't show elapsed for quick calls — below this a tool card reads exactly as before,
 *  so the counter's appearance itself means "this one is taking a while". */
export const SHOW_ELAPSED_AFTER_MS = 2_000;

/**
 * Compact elapsed: `8s` · `1m 04s` · `2h 05m`.
 *
 * Deliberately NOT the DS `duration` formatter, which renders everything ≥1s as
 * `${(ms / 1000).toFixed(1)}s`. That's right for a finished 1.2s call and useless for the
 * 15-minute one this exists to make legible — it would read "900.4s".
 */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total}s`;
  const mins = Math.floor(total / 60);
  if (mins < 60) return `${mins}m ${String(total % 60).padStart(2, "0")}s`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ${String(mins % 60).padStart(2, "0")}m`;
}

/**
 * Milliseconds since `startedAt`, re-rendering about once a second while it is set.
 *
 * `undefined` parks the timer entirely — no interval, no re-render — so a settled card
 * (the overwhelming majority on screen) costs nothing.
 */
export function useElapsed(startedAt: number | undefined): number | undefined {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === undefined) return;
    // Resync on (re)start: the spotlight slot reuses ONE card instance as tools advance
    // (a stable key, to avoid remount strobe), so without this a newly-started tool would
    // briefly render the previous tool's `now`.
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, [startedAt]);
  if (startedAt === undefined) return undefined;
  return Math.max(0, now - startedAt);
}
