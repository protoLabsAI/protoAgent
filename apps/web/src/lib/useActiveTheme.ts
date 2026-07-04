import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api } from "./api";
import { applyAgentTheme } from "./agentTheme";

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
      // default so a reload keeps their look instead of the default clobbering it (#1762). A
      // later switch/save replaces verbatim (ADR 0042) — the agent's saved theme wins.
      applyAgentTheme(q.data.theme, { animate: !first.current, preservePersisted: first.current });
      applied.current = sig;
      first.current = false;
    }
  }, [q.data]);
}
