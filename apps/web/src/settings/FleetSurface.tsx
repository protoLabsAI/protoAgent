import { useEffect, useState } from "react";

import { agentHref } from "../lib/api";
import { useUI } from "../state/uiStore";
import { FleetGroupsPanel, FleetManagerPanel } from "./FleetManagerPanel";
import { NewAgentPanel } from "./NewAgentPanel";

// Global ▸ Fleet (ADR 0042 / 0048). The fleet manager, the new-agent picker and the switcher
// group editor (docs/design/fleet-switcher-groups.md), toggled in place — "+ New agent" opens
// the picker; "Groups" opens the group editor; both return to the list on cancel / back, while
// a successful create navigates INTO the new agent (see onDone below). The FleetSwitcher's
// "+ New agent" deep-link sets a one-shot `fleetStartNew` flag (ADR 0048) so landing here opens
// the picker straight away.
export function FleetSurface() {
  const startNew = useUI((s) => s.fleetStartNew);
  const setStartNew = useUI((s) => s.setFleetStartNew);
  const [view, setView] = useState<"list" | "new" | "groups">(startNew ? "new" : "list");

  useEffect(() => {
    if (startNew) {
      setView("new");
      setStartNew(false); // consume the one-shot so a manual back-to-list sticks
    }
  }, [startNew, setStartNew]);

  if (view === "groups") {
    return <FleetGroupsPanel onBack={() => setView("list")} />;
  }
  if (view === "new") {
    return (
      <NewAgentPanel
        onDone={(_name, id) => {
          // Create lands the operator IN the new agent — a full page load to its slug
          // URL, the same navigation the FleetSwitcher uses (ADR 0042) — because the
          // next move is configuring the agent just made, not re-reading the fleet
          // list. The id is the slug (stable, never the editable display name). A
          // success response without the agent record has no slug to go to → the old
          // back-to-list behavior instead of a navigation to nowhere.
          if (id) window.location.href = agentHref(id);
          else setView("list");
        }}
        onCancel={() => setView("list")}
      />
    );
  }
  return <FleetManagerPanel onNew={() => setView("new")} onGroups={() => setView("groups")} />;
}
