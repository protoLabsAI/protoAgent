// Per-tool-call context cost, estimated client-side (#2282).
//
// A tool card shows name, status, input/output preview and duration — but nothing about
// what the call cost in CONTEXT, so an operator cannot spot the expensive one. A
// `fetch_url` returning 8KB of HTML and a `current_time` returning 40 characters read
// identically, and the first is what eats a context budget.
//
// This is deliberately the CHEAP half of the fix (issue #2282, Option B). Real
// attribution — measuring the token delta a tool result actually adds when it enters the
// context window — needs a counting seam at the tool-execution boundary that the engine
// does not have: usage is tracked per MODEL CALL, and one model call can emit several
// tool calls, all credited to a single usage frame. That's Option A and it stays a
// separate piece of work. What we have on the wire already is the output string, so the
// estimate below costs nothing and answers the primary question ("which call was
// expensive?") today.
//
// It is an ESTIMATE and the UI must never imply otherwise:
//   - chars/4 is a heuristic, not a tokenizer
//   - it does not know how much of the output the model actually reads
//   - it does not know whether compaction trims the result before the next call
//   - the cost lands on the NEXT model call, not the one that made the tool call
// Hence the leading "~" and the explanatory tooltip.

import type { ToolCall } from "../lib/types";

/**
 * Chars per token. Matches the backend's `chars // 4` (`operator_api/prompt_routes.py`,
 * the #2243 P2 prompt-budget rows) on purpose: the two numbers describe the same context
 * window, so an operator comparing a tool card against the prompt viewer's budget
 * breakdown must not see two different arithmetics.
 */
export const CHARS_PER_TOKEN = 4;

/**
 * Below this, the estimate is clutter rather than signal. The chip exists to make an
 * expensive call stand out; stamping "~10" on every `current_time` would defeat that by
 * putting a number on every card. ~250 tokens ≈ 1KB of output — about where a result
 * starts being worth noticing in a context budget.
 */
export const MIN_SHOWN_TOKENS = 250;

/** Rough token count for a tool result. Floor, to match the backend's integer division. */
export function approxTokens(text: string | undefined | null): number {
  if (!text) return 0;
  return Math.floor(text.length / CHARS_PER_TOKEN);
}

/**
 * The estimate to render on a card, or `null` when it should not be shown.
 *
 * Null while the call is still RUNNING: output arrives with the end frame, so a running
 * card's `output` is absent or partial and any number shown would be wrong and would
 * climb for the wrong reason. Null for errors too — a failed call's output is a short
 * message, and "cost" is a misleading frame for a failure.
 */
export function toolCostTokens(call: Pick<ToolCall, "status" | "output" | "outputChars">): number | null {
  if (call.status !== "done") return null;
  // Prefer the server's true pre-truncation size (#2775): `output` on the wire is
  // the card PREVIEW, capped at 800 chars ≈ 200 tokens — strictly below the
  // display floor, which made this chip unreachable from the preview alone. The
  // `output` fallback keeps older servers (no outputChars on the fragment) at the
  // old behavior rather than hiding the chip everywhere.
  const n =
    typeof call.outputChars === "number" ? Math.floor(call.outputChars / CHARS_PER_TOKEN) : approxTokens(call.output);
  return n >= MIN_SHOWN_TOKENS ? n : null;
}
