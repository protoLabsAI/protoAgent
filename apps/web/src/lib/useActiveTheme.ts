import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api } from "./api";
import { applyAgentTheme, persistedThemeIsForCurrentAgent } from "./agentTheme";

// Apply the focused agent's saved theme on boot + on every switch (ADR 0042). The switcher
// invalidates all queries after flipping the active agent, so ["theme"] refetches → the new
// agent's blob → repaint. retry:false so a backend without /api/theme just no-ops to defaults.
export function useActiveTheme() {
  const q = useQuery({ queryKey: ["theme"], queryFn: () => api.getTheme(), retry: false, staleTime: 2_000 });
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
