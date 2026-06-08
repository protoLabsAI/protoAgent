import { BarChart3, Gauge, Puzzle, Settings2 } from "lucide-react";

import { TelemetrySurface } from "../telemetry/TelemetrySurface";
import { DelegatesSection } from "./DelegatesSection";
import { OverviewPanel } from "./OverviewPanel";
import { SettingsCategoryPanel } from "./SettingsCategory";

// Central Settings — only cross-cutting stuff now (the settings decentralization):
// Overview (status), Telemetry, Plugins (delegates + the settings for any view-less
// plugin), and System (runtime/middleware). Agent + Memory settings moved to their
// home views (Agent → Settings, Knowledge → Settings).

export type SettingsTab = "overview" | "telemetry" | "plugins" | "system";

export const SETTINGS_TABS = [
  { id: "overview", label: "Overview", icon: Gauge },
  { id: "telemetry", label: "Telemetry", icon: BarChart3 },
  { id: "plugins", label: "Plugins", icon: Puzzle },
  { id: "system", label: "System", icon: Settings2 },
] as const;

export function SettingsSurface({ tab = "overview" }: { tab?: SettingsTab }) {
  if (tab === "telemetry") return <TelemetrySurface />;       // self-contained (own section + boundary)
  if (tab === "plugins") {
    return (
      <SettingsCategoryPanel
        category="Plugins"
        title="Plugin settings"
        emptyHint="Plugins with their own view manage settings there. Anything view-less shows up here."
        footer={<DelegatesSection />}
      />
    );
  }
  if (tab === "system") return <SettingsCategoryPanel category="System" title="System" />;
  return <OverviewPanel />;                                    // overview (default; own section)
}
