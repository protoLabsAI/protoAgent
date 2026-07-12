import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api, is401, isColdStart } from "./api";
import { applyAgentTheme, persistedThemeIsForCurrentAgent } from "./agentTheme";

/** Retry policy for the theme fetch (#1916): ride out COLD-START failures, give up on
 *  everything else. On FIRST load of a fleet slug window the focused agent may still be
 *  spawning — the theme query fires while `activateSlugAgent()` is resuming the member, so
 *  the hub proxy answers 409/502 (and the desktop sidecar's ~12s boot makes the fetch throw
 *  before any response). The old `retry: false` made that one failed fetch permanent — no
 *  poll and no refetch-on-focus ever re-ran it, so the agent rendered unthemed until a
 *  full-page agent switch reloaded the console with the member warm. Mirrors the
 *  queryClient's cold-start default; everything else stays no-retry so a backend without
 *  /api/theme (404) still no-ops straight to the DS defaults. Exported for unit tests. */
export function themeQueryRetry(failureCount: number, error: unknown): boolean {
  if (is401(error)) return false; // the AuthGate prompt owns recovery (#873)
  return isColdStart(error) && failureCount < 25;
}

// Apply the focused agent's saved theme on boot + on every switch (ADR 0042). Both paths
// converge here: the effect keys on the RESOLVED query data, so the first load applies the
// theme as soon as its fetch lands (riding out a cold agent via themeQueryRetry, #1916) and
// a switch — a full page load in slug routing — is just another boot apply.
export function useActiveTheme() {
  const q = useQuery({ queryKey: ["theme"], queryFn: () => api.getTheme(), retry: themeQueryRetry, staleTime: 2_000 });
  const applied = useRef<string | null>(null);
  const first = useRef(true);
  useEffect(() => {
    if (q.data === undefined) return;
    const sig = JSON.stringify(q.data.theme ?? null);
    if (sig !== applied.current) {
      // Boot apply: no crossfade, and MERGE the user's persisted overrides over this agent's
      // default so a reload keeps their look instead of the default clobbering it (#1762) —
      // but ONLY when the persisted blob belongs to THIS agent. `pl-theme` is a single global
      // localStorage key shared across every same-origin agent window, so a different agent's
      // saved theme can be sitting in it; merging it over this agent would bleed the wrong
      // look (ADR 0042 boot contract). On a mismatch (or later switch/save) we replace verbatim
      // — the focused agent's own saved theme wins.
      const preservePersisted = first.current && persistedThemeIsForCurrentAgent();
      applyAgentTheme(q.data.theme, { animate: !first.current, preservePersisted });
      applied.current = sig;
      first.current = false;
    }
  }, [q.data]);
}
