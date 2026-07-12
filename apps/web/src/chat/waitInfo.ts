// Waiting-state info for the `wait` tool's card (#1914). `wait` (tools/lg_tools.py) ends
// the agent's turn on purpose and schedules a one-shot resume — but its card rendered as a
// generic success, so the user couldn't tell the agent yielded intentionally, for how long,
// or that the chat stays usable meanwhile. Everything the UI needs is already in the tool's
// INPUT args (`{seconds, then}`) — deliberately derived console-side, with NO change to the
// tool's return shape (a failed schedule still returns "Error: …" and stays on the existing
// error renderer).
//
// Defensive by design: the input is the server's JSON *preview*, truncated to 800 chars
// (server/chat.py::_TOOL_PREVIEW_CHARS) — a long `then` can cut the JSON mid-string, and a
// mid-stream card may hold half-written args. Any parse failure → null → the caller falls
// back to the plain success render. Never crash, never guess.

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
