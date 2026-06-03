import { useEffect, useState } from "react";

/**
 * protoLabs.studio brand bumper — a brief splash shown over everything on
 * launch (adapted from ORBIS's IntroSplash). Holds ~2.5s, then hands off to the
 * app via the View Transitions API for a clean cross-fade (plain unmount where
 * the API isn't supported).
 *
 * Brand rule: the wordmark is `protoLabs.studio` (lowercase p, capital L, the
 * `.studio` dot is part of the mark), filled with the brand gradient.
 */

const HOLD_MS = 2500; // entrance + hold before handing off to the app

export function IntroSplash() {
  // Skip under automation (Playwright/Selenium set navigator.webdriver) so the
  // 2.5s overlay doesn't intercept E2E interactions. Real users see it.
  const [gone, setGone] = useState(
    () => typeof navigator !== "undefined" && (navigator as Navigator).webdriver === true,
  );

  useEffect(() => {
    if (gone) return; // skipped (automation) — no timer, nothing to unmount
    const t = window.setTimeout(() => {
      const doc = document as Document & {
        startViewTransition?: (cb: () => void) => unknown;
      };
      if (typeof doc.startViewTransition === "function") {
        // Cross-fade the splash out and the app in (default root transition).
        doc.startViewTransition(() => setGone(true));
      } else {
        setGone(true);
      }
    }, HOLD_MS);
    return () => window.clearTimeout(t);
  }, []);

  if (gone) return null;

  return (
    <div className="intro-splash" role="img" aria-label="protoLabs.studio">
      <div className="intro-splash-rise">
        <img src={`${import.meta.env.BASE_URL}protolabs-icon-outline.svg`} alt="" className="intro-splash-mark" />
        <div className="intro-splash-word">protoLabs.studio</div>
      </div>
    </div>
  );
}
