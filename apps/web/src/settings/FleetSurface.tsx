import { useState } from "react";

import { FleetManagerPanel } from "./FleetManagerPanel";
import { NewAgentPanel } from "./NewAgentPanel";

// Settings → Agents (ADR 0042). The fleet manager + the new-agent picker, toggled in
// place — "+ New agent" opens the picker, which returns to the list on create/cancel.
export function FleetSurface() {
  const [view, setView] = useState<"list" | "new">("list");
  if (view === "new") {
    return <NewAgentPanel onDone={() => setView("list")} onCancel={() => setView("list")} />;
  }
  return <FleetManagerPanel onNew={() => setView("new")} />;
}
