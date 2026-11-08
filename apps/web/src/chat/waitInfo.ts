// Waiting-state info for the `wait` tool's card (#1914). `wait` (tools/scheduler_tools.py) ends
// the agent's turn on purpose and schedules a one-shot resume — but its card rendered as a
// generic success, so the user couldn't tell the agent yielded intentionally, for how long,
// or that the chat stays usable meanwhile. Derive details console-side from the args and
// confirmation without changing the tool's return shape (failed schedules return "Error: …"
// and stay on the existing error renderer).
//
// resolveWaitInfo recovers truncated previews using the tool confirmation.

export type WaitInfo = { seconds: number; then: string };

/** Parse the `wait` tool's input-args preview. Null on anything short of a well-formed
 *  `{seconds: number}` — the signal to fall back to the generic render. */
export function parseWaitInput(input: string | undefined): WaitInfo | null {
  if (!input) return null;
  let p: { seconds?: unknown; then?: unknown };
  try {
    p = JSON.parse(input) as typeof p;
  } catch {
    return null; // truncated preview / mid-stream args
  }
  if (!p || typeof p !== "object" || typeof p.seconds !== "number" || !Number.isFinite(p.seconds)) {
    return null;
  }
  return {
    // The tool clamps to ≥1 (max(1, int(seconds))) — mirror it so "0" never renders.
    seconds: Math.max(1, Math.round(p.seconds)),
    then: typeof p.then === "string" ? p.then : "",
  };
}

/** Recover wait details when the server cuts the input preview at 800 characters. */
export function resolveWaitInfo(
  input: string | undefined,
  output: string | undefined,
): WaitInfo | null {
  if (/^error/i.test(output?.trim() ?? "")) return null;
  const parsed = parseWaitInput(input);
  if (parsed) return parsed;

  const rawSeconds = input?.match(/"seconds"\s*:\s*(-?\d+(?:\.\d+)?)/)?.[1];
  let seconds = rawSeconds === undefined ? undefined : Number(rawSeconds);
  if (seconds === undefined || !Number.isFinite(seconds)) {
    const duration = output?.trim().match(/^Wait scheduled: (.+?)\.(?:\s|$)/)?.[1];
    const units: Record<string, number> = { second: 1, minute: 60, hour: 3600, day: 86400 };
    if (duration && /^(?:\d+(?:\.\d+)?\s+(?:seconds?|minutes?|hours?|days?)\s*)+$/.test(duration)) {
      seconds = 0;
      for (const match of duration.matchAll(/(\d+(?:\.\d+)?)\s+(second|minute|hour|day)s?/g)) {
        seconds += Number(match[1]) * units[match[2]];
      }
    }
  }
  if (seconds === undefined || !Number.isFinite(seconds)) return null;

  const partialThen = input?.match(/"then"\s*:\s*"((?:\\.|[^"\\])*)/)?.[1];
  const then = output?.match(/Will resume to: ([\s\S]*)/)?.[1]
    ?? partialThen?.replace(/\\(["n\\])/g, (_, escaped: string) => escaped === "n" ? "\n" : escaped)
    ?? "";
  return { seconds: Math.max(1, Math.round(seconds)), then };
}

/** Seconds → a short human phrase (300 → "5 minutes"), loosely mirroring the tool's own
 *  `_humanize_duration` so the card and the agent's confirmation speak the same language. */
export function humanizeSeconds(seconds: number): string {
  const s = Math.max(1, Math.round(seconds));
  const unit = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  if (s < 60) return unit(s, "second");
  const mins = Math.floor(s / 60);
  const secs = s % 60;
  if (mins < 60) return secs ? `${unit(mins, "minute")} ${unit(secs, "second")}` : unit(mins, "minute");
  const hours = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem ? `${unit(hours, "hour")} ${unit(rem, "minute")}` : unit(hours, "hour");
}

/** Squash `then` (the agent's self-instruction — can be long/technical, and may itself be
 *  preview-truncated) to a one-line summary: the first sentence when it's short enough,
 *  else a ~`max`-char cut with an ellipsis. */
export function summarizeThen(then: string, max = 120): string {
  const t = then.trim().replace(/\s+/g, " ");
  const sentence = t.match(/^.*?[.!?](?=\s|$)/)?.[0];
  const pick = sentence && sentence.length <= max ? sentence : t;
  return pick.length <= max ? pick : `${pick.slice(0, max - 1).trimEnd()}…`;
}
