// Progressive reveal for a background reply that lands COMPLETE (#4247).
//
// A background delegate's / room participant's answer arrives as ONE `chat.progress` frame
// of kind "room" and is appended whole (serverTurnProgress.applyProgressFrame) — so a
// multi-paragraph reply, often with code blocks, pops in instantly and is hard to parse as
// it lands. This module replays that complete text as a short, purely-presentational,
// client-side reveal so it reads like a live stream, WITHOUT touching the stored message.
//
// The "should this message animate?" bit is deliberately NOT a field on ChatMessage:
// messages persist to the session store, and a persisted flag would re-animate on every
// reload. Instead the reducer marks the id in this module-level registry the moment it
// appends the message; the view CONSUMES the mark on mount. A message loaded from history
// was never marked, so it renders full text at once — the reveal only ever happens on the
// live bus, exactly once per message.

import { useEffect, useRef, useState } from "react";

// ── The one-shot reveal registry ────────────────────────────────────────────────────────
//
// id → expiry epoch-ms. A mark is consumed by the first mount that reads it (takeReveal),
// so the common path never relies on the TTL; the TTL + cap only bound growth if a marked
// message is never mounted (e.g. filtered out of the transcript).
const MARK_TTL_MS = 60_000;
const MARK_CAP = 256;
const marks = new Map<string, number>();

function prune(now: number): void {
  for (const [id, expiry] of marks) {
    if (expiry <= now) marks.delete(id);
  }
  // Map keeps insertion order, so the first keys are the oldest.
  while (marks.size > MARK_CAP) {
    const oldest = marks.keys().next().value;
    if (oldest === undefined) break;
    marks.delete(oldest);
  }
}

/** Mark a message id to reveal progressively the next time it mounts. Idempotent per id
 *  (a repeat just refreshes the TTL). No-op for an empty id. */
export function markForReveal(id: string): void {
  if (!id) return;
  const now = Date.now();
  prune(now);
  marks.set(id, now + MARK_TTL_MS);
}

/** Consume the mark for `id`: returns true (and clears it) if it was marked and unexpired,
 *  false otherwise. Consuming means a later remount renders the full text immediately. */
export function takeReveal(id: string): boolean {
  const expiry = marks.get(id);
  if (expiry === undefined) return false;
  marks.delete(id);
  return expiry > Date.now();
}

/** Test-only: drop all pending marks. */
export function _resetReveals(): void {
  marks.clear();
}

// ── The reveal plan (pure) ───────────────────────────────────────────────────────────────

/** Reveal at ~this many characters per second before clamping. */
export const REVEAL_CHARS_PER_SEC = 600;
/** Shortest total reveal, so a one-line reply still reads as a stream rather than a blink. */
export const REVEAL_MIN_MS = 600;
/** Longest total reveal, so a long answer still finishes promptly. */
export const REVEAL_MAX_MS = 3000;

export interface RevealPlan {
  /** Ascending word-boundary prefix lengths; the last is always `text.length`. Each stop
   *  ends just past the whitespace that follows a word, so a prefix never splits a word —
   *  nor a multi-char marker like a ``` fence, which has no interior whitespace. */
  stops: number[];
  /** Target total reveal duration in ms, clamped to [REVEAL_MIN_MS, REVEAL_MAX_MS]. */
  durationMs: number;
}

/** Word-boundary prefix lengths for `text`. A "word" is a run of non-whitespace plus the
 *  whitespace that trails it, so every interior stop lands after a space/newline. */
function revealStops(text: string): number[] {
  const stops: number[] = [];
  const re = /\S+\s*/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) stops.push(m.index + m[0].length);
  // Empty / whitespace-only text, or a final run with no trailing whitespace captured,
  // still needs the full length as the terminal stop.
  if (stops.length === 0 || stops[stops.length - 1] !== text.length) stops.push(text.length);
  return stops;
}

/** Target reveal duration for a text of `len` characters: ~len/600s, clamped to 0.6–3s. */
export function revealDurationMs(len: number): number {
  return Math.min(REVEAL_MAX_MS, Math.max(REVEAL_MIN_MS, (len / REVEAL_CHARS_PER_SEC) * 1000));
}

/** The whole plan for `text`: its word-boundary stops and its clamped target duration. */
export function revealPlan(text: string): RevealPlan {
  return { stops: revealStops(text), durationMs: revealDurationMs(text.length) };
}

/** How many characters of `text` should be shown after `elapsedMs`. Advances by whole
 *  words: the largest word-boundary stop whose position is within the elapsed fraction of
 *  the text, and the full length once the clamped duration has passed. */
export function nextRevealLength(text: string, elapsedMs: number): number {
  if (!text) return 0;
  const { stops, durationMs } = revealPlan(text);
  if (elapsedMs >= durationMs) return text.length;
  const target = (Math.max(0, elapsedMs) / durationMs) * text.length;
  let shown = 0;
  for (const stop of stops) {
    if (stop <= target) shown = stop;
    else break;
  }
  return shown;
}

// ── The hook ──────────────────────────────────────────────────────────────────────────────

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

function clock(): number {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

/**
 * Progressively reveal `text` for message `id`, once, if it was `markForReveal`-ed before
 * this component mounted (and reduced-motion isn't requested). Returns `{ shown, revealing }`:
 * while `revealing`, `shown` grows from "" to the full text on a rAF loop; otherwise `shown`
 * is the full text and `revealing` is false. If `text` grows mid-reveal (a late append) the
 * reveal continues toward the new full text from the same start instant. Cleans up its frame
 * on unmount.
 */
export function useRevealedText(id: string, text: string): { shown: string; revealing: boolean } {
  // Decide ONCE per mount, from a ref so a StrictMode double-render — or a plain re-render —
  // can't consume the mark twice or flip the decision. A remount after the mark was taken
  // reads false here and renders immediately (history, or a re-mounted bubble).
  const plan = useRef<{ animate: boolean; start: number } | null>(null);
  if (plan.current === null) {
    const animate = takeReveal(id) && !prefersReducedMotion();
    plan.current = { animate, start: animate ? clock() : 0 };
  }
  const animate = plan.current.animate;

  const [len, setLen] = useState<number>(() => (animate ? 0 : text.length));
  // The rAF loop reads the latest full text from a ref, so a text that grows mid-reveal is
  // picked up without resetting progress.
  const textRef = useRef(text);
  textRef.current = text;

  useEffect(() => {
    if (!animate) return;
    let handle = 0;
    const tick = () => {
      const full = textRef.current;
      const n = nextRevealLength(full, clock() - plan.current!.start);
      setLen(n);
      if (n < full.length) handle = window.requestAnimationFrame(tick);
    };
    handle = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(handle);
    // Re-subscribe when the full text changes so a late append resumes the loop toward it;
    // progress is time-based (not reset), so growth stays monotonic.
  }, [animate, text]);

  if (!animate) return { shown: text, revealing: false };
  const shown = text.slice(0, Math.min(len, text.length));
  return { shown, revealing: shown.length < text.length };
}
