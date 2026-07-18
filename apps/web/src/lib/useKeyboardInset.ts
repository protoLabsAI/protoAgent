import { useEffect } from "react";

// Keyboard-aware viewport (mobile). The console shell is a fixed `100dvh` column with
// `body { overflow: hidden }` — so when the on-screen keyboard opens, nothing in the
// layout reacts and the keyboard simply SLIDES OVER the composer. You type blind.
//
// There are two mechanisms and we need both, because neither covers all engines:
//
//   • `interactive-widget=resizes-content` (viewport meta, index.html) — Chrome 108+.
//     The browser shrinks the LAYOUT viewport itself, so `100dvh` already excludes the
//     keyboard and the measurement below naturally lands on ~0. Nothing to do.
//   • `window.visualViewport` (here) — the iOS Safari path, which does NOT support the
//     meta directive and never resizes the layout viewport. We measure the covered
//     region ourselves and publish it as `--kb-inset` for the shell to subtract.
//
// The two compose rather than conflict: whichever engine already handled it reports ~0.
const VAR = "--kb-inset";

// Matches the DS `Conversation` pin threshold (ai.tsx — `dist < 32`), so "was the reader
// at the bottom" means the same thing here as it does there.
const PIN_THRESHOLD = 32;

/** Scrollers that were pinned to the bottom before a viewport change. */
function pinnedScrollers(): HTMLElement[] {
  const out: HTMLElement[] = [];
  // Reaching into the DS scroller by class is deliberate and temporary. The DS
  // `Conversation` re-pins via a ResizeObserver on its CONTENT element (ai.tsx), so a
  // keyboard-driven change to the SCROLLER's height never fires it — the composer lifts
  // but the last message stays hidden behind the keyboard. Superseded once the DS
  // observes the scroll box (protoContent follow-up); drop this then.
  for (const el of document.querySelectorAll<HTMLElement>(".pl-convo-scroll")) {
    if (el.scrollHeight - el.scrollTop - el.clientHeight < PIN_THRESHOLD) out.push(el);
  }
  return out;
}

/**
 * Publishes the keyboard-covered height as `--kb-inset` on <html> and keeps pinned
 * conversation scrollers pinned across the change. No-ops where `visualViewport` is
 * unavailable (desktop Safari <13, jsdom, the Tauri webview on desktop).
 */
export function useKeyboardInset(): void {
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;

    const root = document.documentElement;
    let raf = 0;

    const apply = () => {
      // How much of the layout viewport the keyboard (or any interactive widget) covers.
      // `offsetTop` matters when iOS scrolls the visual viewport within the layout one —
      // without it the inset over-reports while the page is mid-scroll.
      const covered = window.innerHeight - vv.height - vv.offsetTop;
      const inset = Math.max(0, Math.round(covered));

      // Capture pin state BEFORE the height change lands, so the restore below knows
      // whether the reader was actually at the bottom (afterwards it always looks unpinned).
      const pinned = pinnedScrollers();

      root.style.setProperty(VAR, `${inset}px`);

      // The shell resizes on the next frame; re-pin after layout has settled.
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        for (const el of pinned) el.scrollTop = el.scrollHeight;
      });
    };

    apply();
    vv.addEventListener("resize", apply);
    // iOS scrolls the visual viewport (rather than resizing it) when focus moves between
    // fields with the keyboard already up — `resize` alone misses those.
    vv.addEventListener("scroll", apply);
    return () => {
      cancelAnimationFrame(raf);
      vv.removeEventListener("resize", apply);
      vv.removeEventListener("scroll", apply);
      root.style.removeProperty(VAR);
    };
  }, []);
}
