// Pure helpers behind the /trajectory system note (ADR 0102 S2, #2806) — same
// component-free convention as perfView.ts/promptView.ts so formatting stays
// unit-testable. A chat-native read over /api/trajectory/*: what did the model
// see, call by call, and which rewrites (compact/prune/rewind/fork/repair)
// reshaped the history along the way.

import type { TrajectoryCall, TrajectoryEvent } from "../lib/types";

function fmtTokensish(chars: number): string {
  const tokens = Math.floor(chars / 4);
  return tokens >= 1000 ? `~${(tokens / 1000).toFixed(1)}k tok` : `~${tokens} tok`;
}

/** One line per event, newest last — requests as call rows, surface ops as
 *  flagged rewrites, responses folded into their request row upstream. */
export function eventLine(e: TrajectoryEvent): string | null {
  if (e.t === "request") {
    const msgs = e.msgs?.length ?? 0;
    const chars = (e.msgs ?? []).reduce((a, m) => a + (m.chars || 0), 0);
    return `→ call · ${e.model || "model"} · ${msgs} msgs (${fmtTokensish(chars)}) · ${e.tools_count ?? 0} tools`;
  }
  if (e.t === "response") {
    if (e.status === "error") return `← error (${e.error || "unknown"})`;
    const u = e.usage || {};
    const cache = u.cache_read ? ` · ${Math.round((u.cache_read / Math.max(1, u.input || 1)) * 100)}% cached` : "";
    return u.input ? `← ok · ${u.input.toLocaleString()} in / ${(u.output || 0).toLocaleString()} out${cache}` : "← ok";
  }
  if (e.t === "surface_op") {
    const detail = [
      e.removed ? `${e.removed} removed` : "",
      e.kept != null ? `${e.kept} kept` : "",
      e.rewritten_ids?.length ? `${e.rewritten_ids.length} rewritten` : "",
    ]
      .filter(Boolean)
      .join(", ");
    return `⚠ ${e.op}${e.cause ? ` (${e.cause})` : ""}${detail ? ` — ${detail}` : ""}`;
  }
  return null;
}

/** The /trajectory system note: a compact tail timeline + the latest call's
 *  availability readout when provided. Honest about limits — the trajectory
 *  starts when the writer shipped, so older turns simply aren't in it. */
export function trajectoryNoteMarkdown(
  events: TrajectoryEvent[],
  total: number,
  latest: TrajectoryCall | null,
): string {
  if (!events.length) {
    return "**Trajectory**\n\nNothing recorded for this conversation yet — the trajectory starts with the first model call after v0.138.0.";
  }
  const lines = events
    .map(eventLine)
    .filter((l): l is string => l !== null)
    .map((l) => `- ${l}`);
  const shown = lines.length;
  const head = `**Trajectory** — last ${shown} of ${total} events (\`/api/trajectory/…\` has the rest)`;
  let availability = "";
  if (latest?.found) {
    const a = latest.availability;
    availability =
      `\n\nLatest call (#${latest.call + 1}/${latest.calls}): ${latest.messages.length} messages — ` +
      `${a.available} available · ${a.rewritten} rewritten in place · ${a.missing} gone from the live thread ` +
      `(hashes still prove what was sent${a.missing ? "; the chat archive may hold the text" : ""}).`;
  }
  return `${head}\n\n${lines.join("\n")}${availability}`;
}
