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
      applyAgentTheme(q.data.theme, !first.current); // no crossfade on the initial boot apply
      applied.current = sig;
      first.current = false;
    }
  }, [q.data]);
}
