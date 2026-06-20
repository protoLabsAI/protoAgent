import { BarChart3, Bot, BookMarked, Boxes, Database, Gauge, Layers, Network, Palette, Plug, Puzzle, Server, Settings2, Sparkles, Store, Wrench } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, type ReactNode } from "react";

import { SideNav, Tabs } from "@protolabsai/ui/navigation";

import { IdentityPanel } from "../agent/IdentityPanel";
import { McpPanel } from "../app/McpPanel";
import { MiddlewarePanel } from "../app/MiddlewarePanel";
import { SubagentsPanel } from "../app/SubagentsPanel";
import { ToolsPanel } from "../app/ToolsPanel";
import { isHostConsole } from "../lib/api";
import { PluginsSurface } from "../plugins/PluginsSurface";
import { PlaybooksSurface } from "../playbooks/PlaybooksSurface";
import { TelemetrySurface } from "../telemetry/TelemetrySurface";
import { useUI } from "../state/uiStore";
import { DelegatesSection } from "./DelegatesSection";
import { FleetSurface } from "./FleetSurface";
import { OverviewPanel } from "./OverviewPanel";
import { SettingsCategoryPanel } from "./SettingsCategory";
import { ThemeSurface } from "./ThemeSurface";

// Settings IA (ADR 0047/0048 — consolidated 2026-06). There is ONE settings surface: the
// focused agent's settings. "Global" is no longer a separate home — it's simply this surface
// when your focused agent is the host (host-scoped fields are the box defaults every agent
// inherits). The sidenav splits into two labeled groups:
//
//   Agent — everything that defines the focused agent. Host-scoped fields here carry an
//           inheritance badge (ADR 0047): on a fleet member, "inherited from host" + override;
//           on the host console, "box default" (you're setting what others inherit).
//   Box   — box-wide ops (Fleet · Telemetry). Host-console only. (Shared skills live in
//           Agent ▸ Skills — the commons is browsed + shared from there, not a separate panel.)

type Section = { id: string; label: string; icon: LucideIcon; render: () => ReactNode };

// The focused agent's makeup + field settings. Host-scoped fields (model gateway · routing ·
// caching · org · telemetry/fleet runtime) appear inline in Model & Routing / Memory / System
// with their ADR 0047 inheritance badge.
const AGENT_SECTIONS: Section[] = [
  { id: "identity", label: "Identity", icon: Sparkles, render: () => <IdentityPanel /> },
  // id stays "settings" for persisted-section compat; the label is the un-nested name.
  { id: "settings", label: "Model & Routing", icon: Settings2, render: () => <SettingsCategoryPanel category="Agent" title="Model & Routing" /> },
  { id: "plugins", label: "Plugins", icon: Puzzle, render: () => <PluginSettingsHome /> },
  { id: "tools", label: "Tools", icon: Wrench, render: () => <ToolsPanel /> },
  { id: "mcp", label: "MCP", icon: Plug, render: () => <McpPanel /> },
  { id: "subagents", label: "Subagents", icon: Bot, render: () => <SubagentsPanel /> },
  { id: "delegates", label: "Delegates", icon: Network, render: () => <DelegatesSection /> },
  { id: "skills", label: "Skills", icon: BookMarked, render: () => <PlaybooksSurface /> },
  { id: "middleware", label: "Middleware", icon: Layers, render: () => <MiddlewarePanel /> },
  { id: "memory", label: "Memory", icon: Database, render: () => <SettingsCategoryPanel category="Memory" title="Memory" /> },
  { id: "system", label: "System", icon: Settings2, render: () => <SettingsCategoryPanel category="System" title="System" /> },
  { id: "theme", label: "Theme", icon: Palette, render: () => <ThemeSurface /> },
];

// Box-wide operations (host console only) — the former Global ▸ Fleet/Telemetry.
// The old Global ▸ Configuration section is GONE: host-scoped FIELDS are edited inline in the
// Agent group (on the host they write the host layer; elsewhere they override per-agent).
// Shared Skills folded into Agent ▸ Skills (PlaybooksSurface) — it already browses the
// commons (tier badges) and shares/unshares from there, so a separate panel was redundant.
const BOX_SECTIONS: Section[] = [
  { id: "overview", label: "Overview", icon: Gauge, render: () => <OverviewPanel /> },
  { id: "fleet", label: "Fleet", icon: Server, render: () => <FleetSurface /> },
  { id: "telemetry", label: "Telemetry", icon: BarChart3, render: () => <TelemetrySurface /> },
];

// The Plugins manager (install · enable · configure, plus the Discover directory) lives
// in Settings ▸ Plugins. Per-plugin config is inline per row (ADR 0059); the delegate
// registry is built-in core infrastructure with its own Delegates section.
function PluginSettingsHome() {
  const pluginsTab = useUI((s) => s.pluginsTab);
  const setPluginsTab = useUI((s) => s.setPluginsTab);
  return (
    <>
      <Tabs
        responsive
        active={pluginsTab}
        onSelect={(t) => setPluginsTab(t as "local" | "market")}
        items={[
          { id: "local", label: "Installed", icon: <Boxes size={15} /> },
          { id: "market", label: "Discover", icon: <Store size={15} /> },
        ]}
      />
      <PluginsSurface tab={pluginsTab} />
    </>
  );
}

// One consolidated settings surface. `only` is accepted but ignored (legacy callers) — there
// is a single home now; the Box group is gated to the host console. `initialSection`
// deep-links a section (the overlay / a ⌘K command).
export function SettingsSurface({ initialSection }: { only?: "host" | "workspace"; initialSection?: string } = {}) {
  const onHost = isHostConsole();
  const persistedSection = useUI((s) => s.settingsSection);
  const setSection = useUI((s) => s.setSettingsSection);

  // Deep-link: select the requested section once when opened on one (overlay / palette).
  useEffect(() => {
    if (initialSection) setSection(initialSection);
  }, [initialSection, setSection]);

  const sections = onHost ? [...AGENT_SECTIONS, ...BOX_SECTIONS] : AGENT_SECTIONS;
  const active = sections.find((s) => s.id === persistedSection) ?? sections[0];
  const toItem = (s: Section) => ({ id: s.id, label: s.label, icon: <s.icon size={15} /> });
  const groups = [
    { label: "Agent", items: AGENT_SECTIONS.map(toItem) },
    ...(onHost ? [{ label: "Box", items: BOX_SECTIONS.map(toItem) }] : []),
  ];

  return (
    <div className="settings-shell">
      <SideNav ariaLabel="Settings sections" groups={groups} active={active.id} onSelect={(id) => setSection(id)} />
      <div className="settings-content">
        {active.render()}
      </div>
    </div>
  );
}
