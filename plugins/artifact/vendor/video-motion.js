/**
 * VENDORED — @protolabsai/design@0.14.0, src/video-motion.js, byte-for-byte.
 *
 * Relative dependency of tokens.js; keep this flat sibling allowlisted in _routes.py.
 * DO NOT EDIT BY HAND — copy verbatim from the package.
 * tests/test_artifact_plugin.py guards package drift and the module dependency closure.
 */
/**
 * @protolabsai/design — video-motion helper (protoLabs bookends v1, #606 slice 1).
 *
 * THE single pure source for the canonical video easing and the fixed bookend
 * durations. `src/tokens.js` imports the generated values from here; this file
 * imports NOTHING (not tokens, not a renderer) — just arithmetic — so there is
 * no cycle and no runtime dependency. Keeping the easing math in one pure place
 * is what lets the exact runtime curve and the CSS approximation stay derived
 * from the same function instead of hand-copied and drifting apart.
 *
 * Two forms of the curve ship, and they are NOT interchangeable:
 *   • {@link videoEase} — the EXACT easeInOutCubic, for runtime/JS-driven motion.
 *   • {@link videoEaseLinearStops} — a piecewise-linear CSS `linear()` that
 *     APPROXIMATES videoEase from 65 uniform samples. The browser interpolates
 *     straight lines between the stops, so it is a faithful approximation, never
 *     the exact cubic — and it is deliberately NOT a `cubic-bezier()` lookalike.
 */

/** Fixed bookend durations, in milliseconds. Frozen — these are canonical. */
export const VIDEO_MOTION_MS = Object.freeze({
  shot: 600, // Edge Sweep — the primary "shot" duration
  bumperFade: 300, // bumper fade in / out
});

/**
 * Number of `linear()` samples: 65 uniform points at i/64 for i = 0…64, i.e.
 * 64 equal intervals with both endpoints included.
 */
export const VIDEO_EASE_SAMPLES = 65;

/**
 * Canonical video easing — the EXACT easeInOutCubic over a finite, clamped
 * progress u ∈ [0, 1]:
 *
 *   E(u) = 4·u³                  for u < 0.5
 *        = 1 − (−2u + 2)³ / 2    for u ≥ 0.5
 *
 * Progress is clamped into [0, 1] before evaluation, so values that stray past
 * the edges saturate rather than overshoot. A non-finite input (NaN, ±Infinity)
 * or non-number is rejected outright — silently returning garbage for those is
 * how a renderer ends up animating to NaN.
 *
 * This is the RUNTIME curve. It is exact; do not confuse it with the CSS
 * `linear()` approximation emitted by {@link videoEaseLinearStops}.
 *
 * @param {number} u  progress in [0, 1] (values outside the range are clamped)
 * @returns {number}  eased value in [0, 1]
 */
export function videoEase(u) {
  if (typeof u !== "number" || !Number.isFinite(u)) {
    throw new TypeError(`videoEase: progress must be a finite number, got ${String(u)}`);
  }
  const t = u < 0 ? 0 : u > 1 ? 1 : u;
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/**
 * The raw easing samples as numbers: videoEase(i/(n-1)) for i = 0…n-1. Exposed
 * so callers (and the tests) can reason about the curve without string-parsing.
 *
 * @param {number} [samples=VIDEO_EASE_SAMPLES]  stop count (>= 2)
 * @returns {number[]}  `samples` eased values, from E(0)=0 to E(1)=1
 */
export function videoEaseSamples(samples = VIDEO_EASE_SAMPLES) {
  if (!Number.isInteger(samples) || samples < 2) {
    throw new RangeError(`videoEaseSamples: need an integer >= 2, got ${String(samples)}`);
  }
  const n = samples - 1;
  const out = [];
  for (let i = 0; i <= n; i++) out.push(videoEase(i / n));
  return out;
}

/**
 * Serialize the canonical easing as a CSS `linear()` easing function built from
 * {@link VIDEO_EASE_SAMPLES} uniform samples (i/64). The two endpoints are
 * emitted EXACTLY as `0` and `1`; every interior stop is serialized at 8 decimal
 * places (>= 8-dp precision), which keeps the piecewise-linear reconstruction
 * within ~3.6e-4 of the exact cubic across the whole range — comfortably under
 * the 4e-4 budget, with rounding error (< 5e-9/stop) negligible beside it.
 *
 * Emitted verbatim into `--pl-motion-ease-video`; a faithful APPROXIMATION of
 * {@link videoEase}, generated from that same function — not the exact curve.
 *
 * @param {number} [samples=VIDEO_EASE_SAMPLES]  stop count (>= 2)
 * @returns {string}  e.g. `linear(0, 0.00001526, …, 1)`
 */
export function videoEaseLinearStops(samples = VIDEO_EASE_SAMPLES) {
  const ys = videoEaseSamples(samples);
  const last = ys.length - 1;
  const stops = ys.map((y, i) => {
    if (i === 0) return "0"; // exact start
    if (i === last) return "1"; // exact end
    return y.toFixed(8); // >= 8-dp interior stop
  });
  return `linear(${stops.join(", ")})`;
}
