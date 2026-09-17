import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { PanelHeader } from "@protolabsai/ui/navigation";
import { useToast } from "@protolabsai/ui/overlays";
import { Button } from "@protolabsai/ui/primitives";
import { ThemePanel } from "@protolabsai/ui/theming";

import { api } from "../lib/api";
import { applyAgentTheme, currentThemeBlob } from "../lib/agentTheme";

// Settings → Theme (ADR 0042). The DS ThemePanel edits the look live (it persists to its own
// localStorage); "Save to this agent" PUTs that blob to /api/theme so the FOCUSED agent keeps
// it, and the switch repaints automatically (useActiveTheme). The blob is opaque to us.
export function ThemeSurface() {
  const qc = useQueryClient();
  const toast = useToast();
  // The panel reads the persisted blob once, on mount. Reset clears it underneath the
  // mounted panel, so remount the panel afterwards — otherwise it keeps showing (and on
  // the next toggle or edit, re-saves) the look that was just reset.
  const [panelKey, setPanelKey] = useState(0);

  const save = useMutation({
    mutationFn: () => api.saveTheme(currentThemeBlob()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["theme"] });
      toast({ tone: "success", title: "Theme saved", message: "This agent will keep this look." });
    },
    onError: (e: Error) => toast({ tone: "error", title: "Save failed", message: e.message }),
  });

  const reset = useMutation({
    mutationFn: () => api.resetTheme(),
    onSuccess: () => {
      applyAgentTheme(null);
      setPanelKey((k) => k + 1);
      qc.invalidateQueries({ queryKey: ["theme"] });
      toast({ tone: "success", title: "Theme reset", message: "Back to the defaults." });
    },
    onError: (e: Error) => toast({ tone: "error", title: "Reset failed", message: e.message }),
  });

  return (
    <section className="panel stage-panel">
      <PanelHeader
        title="Theme"
        kicker="this agent's look — saved per agent, repaints when you switch"
        actions={
          <>
            <Button variant="ghost" onClick={() => reset.mutate()} disabled={reset.isPending}>
              Reset
            </Button>
            <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending}>
              Save to this agent
            </Button>
          </>
        }
      />
      <div className="stage-body">
        <ThemePanel key={panelKey} />
      </div>
    </section>
  );
}
