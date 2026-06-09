import { useEffect, useState } from "react";
import { ProtoLabsIcon } from "./ProtoLabsIcon";

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
const SEEN_KEY = "protoagent.introSeen"; // sessionStorage — show the bumper once per tab session

// Skip the splash when: (a) under automation (Playwright sets navigator.webdriver) so the 2.5s
// overlay doesn't intercept E2E, or (b) it's already played this session — so a refresh doesn't
// replay it. sessionStorage clears when the tab closes, so a fresh session sees it once.
function alreadySeen(): boolean {
  if (typeof navigator !== "undefined" && (navigator as Navigator).webdriver === true) return true;
  try {
    return sessionStorage.getItem(SEEN_KEY) === "1";
  } catch {
    return false; // private mode / storage blocked — fall through and show it
  }
}

export function IntroSplash() {
  const [gone, setGone] = useState(alreadySeen);

  useEffect(() => {
    if (gone) return; // skipped (automation or already seen) — no timer, nothing to unmount
    // Mark it seen immediately so a refresh *during* the hold also skips it.
    try {
      sessionStorage.setItem(SEEN_KEY, "1");
    } catch {
      /* storage blocked — it'll just replay next refresh */
    }
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
        <ProtoLabsIcon variant="outline" size={88} className="intro-splash-mark" decorative />
        <div className="intro-splash-word">protoLabs.studio</div>
      </div>
    </div>
  );
}
