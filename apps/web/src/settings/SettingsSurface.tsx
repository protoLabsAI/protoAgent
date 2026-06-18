import { Bot, BookMarked, Boxes, Database, Gauge, HardDrive, Layers, Palette, Plug, Puzzle, Settings2, Sparkles, Wrench } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { PanelHeader, SideNav, Tabs } from "@protolabsai/ui/navigation";
import { Button } from "@protolabsai/ui/primitives";

import { IdentityPanel } from "../agent/IdentityPanel";
import { McpPanel } from "../app/McpPanel";
import { MiddlewarePanel } from "../app/MiddlewarePanel";
import { SubagentsPanel } from "../app/SubagentsPanel";
import { ToolsPanel } from "../app/ToolsPanel";
import { isHostConsole } from "../lib/api";
import { PlaybooksSurface } from "../playbooks/PlaybooksSurface";
import { useUI, type SettingsScope } from "../state/uiStore";
import { DelegatesSection } from "./DelegatesSection";
import { OverviewPanel } from "./OverviewPanel";
import { HostConfigLocked, HostDefaultsPanel, SettingsCategoryPanel } from "./SettingsCategory";
import { ThemeSurface } from "./ThemeSurface";

// Settings IA (ADR 0048 + PR4) — scope is the primary axis. TWO homes, each with its
// own section sub-nav:
//
//   🖥 Global       — the box-shared cascade ONLY (ADR 0047): Overview + Configuration
//                     (the host-scoped fields). Editable on the host console; a
//                     workspace console sees them read-only. Set once, every agent
//                     inherits; per-agent overrides win.
//   🧩 Workspace    — the focused agent: everything that defines it.
//
// Box-level *tools* that aren't cascade settings — Fleet, Telemetry, Commons — moved
// OUT of Global to the dedicated Box rail surface (src/app/BoxSurface.tsx, PR4).

type Section = { id: string; label: string; icon: LucideIcon; render: () => ReactNode };
type Home = { id: SettingsScope; label: string; icon: LucideIcon; sections: Section[] };

// Global = the box-shared cascade ONLY (ADR 0047): Overview + the host-scoped fields.
// Fleet / Telemetry / Commons were box-level *tools*, not cascade settings — moved out
// to the Box rail surface (PR4). Host config is gated to the host console; a workspace
// console sees a read-only pointer instead (ADR 0047 §7.7).
const HOST_SECTIONS: Section[] = [
  { id: "overview", label: "Overview", icon: Gauge, render: () => <OverviewPanel /> },
  // The host-scoped FIELDS (model gateway · routing · caching · org + the commons
  // location) in ONE panel, saving to the box's host-config.yaml (ADR 0047).
  { id: "config", label: "Configuration", icon: HardDrive, render: () => (isHostConsole() ? <HostDefaultsPanel title="Configuration" /> : <HostConfigLocked />) },
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
    render: () => <PluginSettingsHome />,
  },
];

// ADR 0059 — per-plugin config now lives with each plugin in the Plugins panel
// (Installed → Configure). This section points there + keeps the delegate registry
// (which has its own richer UI, not generic settings fields).
function PluginSettingsHome() {
  const setSurface = useUI((s) => s.setSurface);
  const setPluginsTab = useUI((s) => s.setPluginsTab);
  const openPlugins = () => { setPluginsTab("local"); setSurface("plugins"); };
  return (
    <>
      <PanelHeader title="Plugins" kicker="manage + configure in the Plugins panel" />
      <div className="stage-body">
        <p className="muted" style={{ marginTop: 0 }}>
          Browse, install, enable, and <strong>configure</strong> plugins in the dedicated Plugins panel —
          each installed plugin's settings now live with it (Installed → <strong>Configure</strong>).
        </p>
        <Button variant="primary" type="button" onClick={openPlugins}>
          <Puzzle size={15} /> Open Plugins
        </Button>
        <DelegatesSection />
      </div>
    </>
  );
}

export const SETTINGS_HOMES: Home[] = [
  { id: "host", label: "Global", icon: HardDrive, sections: HOST_SECTIONS },
  { id: "workspace", label: "Workspace", icon: Boxes, sections: WORKSPACE_SECTIONS },
];

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

  // Two-column settings shell: the DS SideNav rail (scope toggle pinned in its header
  // slot + the sections down the side, ADR 0048) + the active section's panel filling
  // the rest. Adopted from the interim hand-rolled rail once SideNav shipped in
  // @protolabsai/ui 0.30.0 (protoContent#225 → #227). We deliberately DON'T pass
  // `responsive`: its collapse-to-<select> fires at wrap ≤ 15rem, but our compact
  // in-rail settings column sits below that, so it would render as a dropdown instead
  // of the vertical nav — the opposite of the ask. The rail stays vertical in both the
  // overlay and the stage; the narrow case is the separate mobile shell (ADR 0035 S4).
  return (
    <div className="settings-shell">
      <SideNav
        ariaLabel="Settings sections"
        active={active.id}
        onSelect={(t) => setSection(t)}
        items={home.sections.map((s) => ({ id: s.id, label: s.label, icon: <s.icon size={15} /> }))}
        // Scope toggle pinned atop the rail; text-only so both home labels fit.
        header={
          <Tabs
            variant="segmented"
            ariaLabel="Settings scope"
            active={scope}
            onSelect={(t) => selectHome(t as SettingsScope)}
            items={SETTINGS_HOMES.map((h) => ({ id: h.id, label: h.label }))}
          />
        }
      />
      <div className="settings-content">{active.render()}</div>
    </div>
  );
}
