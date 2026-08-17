import { useEffect, useRef, useState } from "react";

/**
 * Reveal `text` as a steadily-advancing prefix while `active` — the source-space
 * smoothing half of the streaming fade (#2769).
 *
 * Why source-space: streamdown's animate plugin marks "already visible" content by a
 * flat character offset in TREE-VISIT order, and GFM restructuring (footnote sections
 * hoist to the document bottom; remend closes half-arrived syntax) reorders the tree
 * mid-stream — so content shifts across that offset and the wrong spans animate
 * (later lines fading while earlier ones still arrive; footnote links popping in
 * unfaded). A prefix of the SOURCE, only ever advancing, is append-only by
 * construction: the parse tail is the only thing changing, so the fade always lands
 * on genuinely-new words.
 *
 * Rate: per animation frame, advance by `max(MIN_STEP, backlog * CATCH_UP)` chars —
 * proportional catch-up absorbs bursty chunk arrival (~24 chars / ~100ms from the
 * wire) into a steady flow without ever falling far behind a fast model. When
 * `active` is false (message settled, or a historical render) the full text shows
 * immediately — no typewriter replay.
 */
const MIN_STEP = 1.5; // chars/frame floor ≈ 90 cps at 60fps — reading-speed trickle
const CATCH_UP = 0.12; // fraction of the backlog consumed per frame ≈ 120ms catch-up half-life

export function useSmoothReveal(text: string, active: boolean): string {
  const [shownLen, setShownLen] = useState(() => (active ? 0 : text.length));
  const shownRef = useRef(shownLen);
  shownRef.current = Math.min(shownRef.current, text.length); // a shrink (terminal REPLACE) never strands the cursor

  useEffect(() => {
    if (!active) {
      shownRef.current = text.length;
      setShownLen(text.length);
      return;
    }
    if (shownRef.current >= text.length) return;
    let raf = 0;
    const tick = () => {
      const backlog = text.length - shownRef.current;
      if (backlog <= 0) return;
      shownRef.current = Math.min(text.length, shownRef.current + Math.max(MIN_STEP, backlog * CATCH_UP));
      setShownLen(Math.floor(shownRef.current));
      if (shownRef.current < text.length) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [text, active]);

  return active ? text.slice(0, shownLen) : text;
}
