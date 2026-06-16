import { useEffect, useState } from "react";

import { useUI } from "../state/uiStore";
import { FleetManagerPanel } from "./FleetManagerPanel";
import { NewAgentPanel } from "./NewAgentPanel";

// Global ▸ Fleet (ADR 0042 / 0048). The fleet manager + the new-agent picker,
// toggled in place — "+ New agent" opens the picker, which returns to the list on
// create/cancel. The FleetSwitcher's "+ New agent" deep-link sets a one-shot
// `fleetStartNew` flag (ADR 0048) so landing here opens the picker straight away.
export function FleetSurface() {
  const startNew = useUI((s) => s.fleetStartNew);
  const setStartNew = useUI((s) => s.setFleetStartNew);
  const [view, setView] = useState<"list" | "new">(startNew ? "new" : "list");

  useEffect(() => {
    if (startNew) {
      setView("new");
      setStartNew(false); // consume the one-shot so a manual back-to-list sticks
    }
  }, [startNew, setStartNew]);

  if (view === "new") {
    return <NewAgentPanel onDone={() => setView("list")} onCancel={() => setView("list")} />;
  }
  return <FleetManagerPanel onNew={() => setView("new")} />;
}
