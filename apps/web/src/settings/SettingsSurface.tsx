import { BarChart3, Bot, BookMarked, Boxes, Database, Gauge, HardDrive, Layers, Library, Palette, Plug, Puzzle, Server, Settings2, Sparkles, Wrench } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Tabs } from "@protolabsai/ui/navigation";

import { IdentityPanel } from "../agent/IdentityPanel";
import { McpPanel } from "../app/McpPanel";
import { MiddlewarePanel } from "../app/MiddlewarePanel";
import { SubagentsPanel } from "../app/SubagentsPanel";
import { ToolsPanel } from "../app/ToolsPanel";
import { PlaybooksSurface } from "../playbooks/PlaybooksSurface";
import { useUI, type SettingsScope } from "../state/uiStore";
import { TelemetrySurface } from "../telemetry/TelemetrySurface";
import { CommonsPanel } from "./CommonsPanel";
import { DelegatesSection } from "./DelegatesSection";
import { FleetSurface } from "./FleetSurface";
import { OverviewPanel } from "./OverviewPanel";
import { HostDefaultsPanel, SettingsCategoryPanel } from "./SettingsCategory";
import { ThemeSurface } from "./ThemeSurface";

// Settings IA (ADR 0048) — scope is the primary axis. TWO homes, each with its own
// section sub-nav, replacing the old flat category tabs:
//
//   🖥 Host / App   — box-shared, reachable from any workspace (set once, every agent
//                     inherits; per-agent overrides win, ADR 0047).
//   🧩 Workspace    — the focused agent: everything that defines it.
//
// S-A builds the Host/App home in full and a transitional Workspace home (today's
// agent-scoped central settings). S-B folds the agent makeup (Identity/Tools/MCP/
// Subagents/Skills/Middleware + Model/Behavior) into Workspace; S-C removes the
// standalone Agent rail surface + the old category tabs.

type Section = { id: string; label: string; icon: LucideIcon; render: () => ReactNode };
type Home = { id: SettingsScope; label: string; icon: LucideIcon; sections: Section[] };

const HOST_SECTIONS: Section[] = [
  { id: "overview", label: "Overview", icon: Gauge, render: () => <OverviewPanel /> },
  // The host-scoped FIELDS (model gateway · routing · caching · org + the commons
  // location) in ONE panel, saving to the box's host-config.yaml (ADR 0047).
  { id: "config", label: "Host config", icon: HardDrive, render: () => <HostDefaultsPanel title="Host configuration" /> },
  { id: "fleet", label: "Fleet", icon: Server, render: () => <FleetSurface /> },
  { id: "telemetry", label: "Telemetry", icon: BarChart3, render: () => <TelemetrySurface /> },
  // The box-shared skill commons (ADR 0041): browse it, promote from a workspace.
  { id: "commons", label: "Commons", icon: Library, render: () => <CommonsPanel /> },
];

// The Workspace home (ADR 0048 §3.2) — the focused agent, everything that defines it:
// the makeup panels (Identity/Tools/MCP/Subagents/Skills/Middleware) folded in from the
// old Agent rail surface + Theme, plus the agent-scoped field settings (Agent/Memory/
// System/Plugins categories). Host-scoped fields that appear here (e.g. model.name)
// keep their ADR 0047 "inherited from Host" badge + override. (A later refinement can
// regroup the field sections into the ADR's idealized Model/Behavior cut.)
const WORKSPACE_SECTIONS: Section[] = [
  { id: "identity", label: "Identity", icon: Sparkles, render: () => <IdentityPanel /> },
  { id: "settings", label: "Settings", icon: Settings2, render: () => <SettingsCategoryPanel category="Agent" title="Agent settings" /> },
  { id: "tools", label: "Tools", icon: Wrench, render: () => <ToolsPanel /> },
  { id: "mcp", label: "MCP", icon: Plug, render: () => <McpPanel /> },
  { id: "subagents", label: "Subagents", icon: Bot, render: () => <SubagentsPanel /> },
  { id: "skills", label: "Skills", icon: BookMarked, render: () => <PlaybooksSurface /> },
  { id: "middleware", label: "Middleware", icon: Layers, render: () => <MiddlewarePanel /> },
  { id: "memory", label: "Memory", icon: Database, render: () => <SettingsCategoryPanel category="Memory" title="Memory" /> },
  { id: "system", label: "System", icon: Settings2, render: () => <SettingsCategoryPanel category="System" title="System" /> },
  { id: "theme", label: "Theme", icon: Palette, render: () => <ThemeSurface /> },
  {
    id: "plugins",
    label: "Plugins",
    icon: Puzzle,
    render: () => (
      <SettingsCategoryPanel
        category="Plugins"
        title="Plugin settings"
        emptyHint="Plugins with their own view manage settings there. Anything view-less shows up here."
        footer={<DelegatesSection />}
      />
    ),
  },
];

export const SETTINGS_HOMES: Home[] = [
  { id: "host", label: "Host / App", icon: HardDrive, sections: HOST_SECTIONS },
  { id: "workspace", label: "Workspace", icon: Boxes, sections: WORKSPACE_SECTIONS },
];

// Interim vertical section nav — DS `Tabs` is horizontal-only, and the Workspace
// home's 11 sections overflow/read as "intense" in a strip. Filed as the DS
// `SideNav` gap (protoContent#225); the adoption sweep retires this when it ships.
function SectionNav({
  sections,
  active,
  onSelect,
}: {
  sections: Section[];
  active: string;
  onSelect: (id: string) => void;
}) {
  return (
    <nav className="settings-sidenav-list" aria-label="Settings sections">
      {sections.map((s) => (
        <button
          key={s.id}
          type="button"
          className={`settings-sidenav-item${s.id === active ? " is-active" : ""}`}
          aria-current={s.id === active ? "page" : undefined}
          onClick={() => onSelect(s.id)}
        >
          <s.icon size={15} />
          <span>{s.label}</span>
        </button>
      ))}
    </nav>
  );
}

export function SettingsSurface() {
  const scope = useUI((s) => s.settingsScope);
  const section = useUI((s) => s.settingsSection);
  const setScope = useUI((s) => s.setSettingsScope);
  const setSection = useUI((s) => s.setSettingsSection);

  const home = SETTINGS_HOMES.find((h) => h.id === scope) ?? SETTINGS_HOMES[0];
  const active = home.sections.find((s) => s.id === section) ?? home.sections[0];

  // Switching home lands on its first section unless the current section id also
  // exists in the new home (it usually won't — the homes don't share section ids).
  function selectHome(next: SettingsScope) {
    const h = SETTINGS_HOMES.find((x) => x.id === next) ?? SETTINGS_HOMES[0];
    setScope(next);
    if (!h.sections.some((s) => s.id === section)) setSection(h.sections[0].id);
  }

  // Two-column settings shell (replaces the stacked horizontal tab strips): a left
  // rail with the scope toggle pinned on top + the section list vertical below it,
  // and the active section's panel filling the rest. Works at the overlay's 960px
  // and as a full stage surface; the rail collapses to a top strip when narrow (CSS).
  return (
    <div className="settings-shell">
      <aside className="settings-sidenav">
        {/* Scope toggle: text-only (no icons) so "Host / App" + "Workspace" fit the
            narrow rail without clipping; the sections below carry the icons. */}
        <Tabs
          variant="segmented"
          ariaLabel="Settings scope"
          active={scope}
          onSelect={(t) => selectHome(t as SettingsScope)}
          items={SETTINGS_HOMES.map((h) => ({ id: h.id, label: h.label }))}
        />
        <SectionNav sections={home.sections} active={active.id} onSelect={(t) => setSection(t)} />
      </aside>
      <div className="settings-content">{active.render()}</div>
    </div>
  );
}
