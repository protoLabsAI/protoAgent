import { Button } from "@protolabsai/ui/primitives";
import { Dialog } from "@protolabsai/ui/overlays";
import { Palette } from "lucide-react";
import { useState } from "react";

import { ThemeSurface } from "./ThemeSurface";

// Theme quick-set (ADR 0048, operator direction) — a palette icon → dialog with the
// per-agent appearance controls (the same ThemeSurface that lives under Settings ▸
// Workspace ▸ Theme). Theme isn't a FIELDS-backed setting (it persists its own blob
// via /api/theme), so it gets a dedicated quick dialog rather than a QuickSetting.
export function ThemeQuickButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button icon variant="ghost" type="button" title="Appearance" aria-label="Appearance" onClick={() => setOpen(true)}>
        <Palette size={16} />
      </Button>
      {open ? (
        <Dialog open onClose={() => setOpen(false)} title="Appearance" width="min(720px, 94vw)" className="theme-quick-dialog">
          <ThemeSurface />
        </Dialog>
      ) : null}
    </>
  );
}
