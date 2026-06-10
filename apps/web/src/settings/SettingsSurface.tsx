import { BarChart3, Gauge, HardDrive, Palette, Puzzle, Server, Settings2 } from "lucide-react";

import { TelemetrySurface } from "../telemetry/TelemetrySurface";
import { DelegatesSection } from "./DelegatesSection";
import { FleetSurface } from "./FleetSurface";
import { OverviewPanel } from "./OverviewPanel";
import { HostDefaultsPanel, SettingsCategoryPanel } from "./SettingsCategory";
import { ThemeSurface } from "./ThemeSurface";

// Central Settings — only cross-cutting stuff now (the settings decentralization):
// Overview (status), Telemetry, Plugins (delegates + the settings for any view-less
// plugin), and System (runtime/middleware). Agent + Memory settings moved to their
// home views (Agent → Settings, Knowledge → Settings).

export type SettingsTab = "overview" | "agents" | "theme" | "telemetry" | "plugins" | "system" | "host";

export const SETTINGS_TABS = [
  { id: "overview", label: "Overview", icon: Gauge },
  { id: "agents", label: "Agents", icon: Server },
  { id: "theme", label: "Theme", icon: Palette },
  { id: "telemetry", label: "Telemetry", icon: BarChart3 },
  { id: "plugins", label: "Plugins", icon: Puzzle },
  { id: "system", label: "System", icon: Settings2 },
  // Host / box-shared defaults (ADR 0047) — the host-scoped subset, edited at the host layer.
  { id: "host", label: "Host defaults", icon: HardDrive },
] as const;

export function SettingsSurface({ tab = "overview" }: { tab?: SettingsTab }) {
  if (tab === "agents") return <FleetSurface />;              // the fleet (ADR 0042)
  if (tab === "theme") return <ThemeSurface />;               // per-agent look (ADR 0042)
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
  if (tab === "host") return <HostDefaultsPanel />;            // box-shared defaults (ADR 0047)
  return <OverviewPanel />;                                    // overview (default; own section)
}
