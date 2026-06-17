import { BarChart3, Library, Server } from "lucide-react";

import { Tabs } from "@protolabsai/ui/navigation";

import { CommonsPanel } from "../settings/CommonsPanel";
import { FleetSurface } from "../settings/FleetSurface";
import { useUI, type BoxTab } from "../state/uiStore";
import { TelemetrySurface } from "../telemetry/TelemetrySurface";

// The Box surface (PR4 / ADR 0048 §5) — box-level operations that are NOT per-agent
// cascade settings: the fleet roster, the telemetry dashboard, and the skill commons.
// They used to live as sections under Settings ▸ Global, which conflated "box-shared
// settings" (the cascade) with "box-level tools." Now they're their own rail surface
// and Settings ▸ Global is just the cascade (Overview + Configuration).
const BOX_TABS: { id: BoxTab; label: string; icon: typeof Server }[] = [
  { id: "fleet", label: "Fleet", icon: Server },
  { id: "telemetry", label: "Telemetry", icon: BarChart3 },
  { id: "commons", label: "Commons", icon: Library },
];

export function BoxSurface() {
  const tab = useUI((s) => s.boxTab);
  const setTab = useUI((s) => s.setBoxTab);
  return (
    <>
      <Tabs
        responsive
        ariaLabel="Box sections"
        active={tab}
        onSelect={(t) => setTab(t as BoxTab)}
        items={BOX_TABS.map((t) => ({ id: t.id, label: t.label, icon: <t.icon size={15} /> }))}
      />
      {tab === "fleet" ? <FleetSurface /> : tab === "telemetry" ? <TelemetrySurface /> : <CommonsPanel />}
    </>
  );
}
