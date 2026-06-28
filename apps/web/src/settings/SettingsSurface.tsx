import { BarChart3, Bot, BookMarked, Boxes, Brain, Cpu, Database, Gauge, Keyboard, MessageSquare, Network, Palette, Plug, Puzzle, Server, Share2, SlidersHorizontal, Sparkles, Store, Wrench } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, type ReactNode } from "react";

import { SideNav, Tabs } from "@protolabsai/ui/navigation";

import { IdentityPanel } from "../agent/IdentityPanel";
import { McpPanel } from "../app/McpPanel";
import { SubagentsPanel } from "../app/SubagentsPanel";
import { ToolsPanel } from "../app/ToolsPanel";
import { isHostConsole } from "../lib/api";
import { PluginsSurface } from "../plugins/PluginsSurface";
import { PlaybooksSurface } from "../playbooks/PlaybooksSurface";
import { TelemetrySurface } from "../telemetry/TelemetrySurface";
import { useUI } from "../state/uiStore";
import { DelegatesSection } from "./DelegatesSection";
import { FleetSurface } from "./FleetSurface";
import { KeybindingsPanel } from "./KeybindingsPanel";
import { ChatSettingsPanel } from "./ChatSettingsPanel";
import { OverviewPanel } from "./OverviewPanel";
import { SettingsCategoryPanel } from "./SettingsCategory";
import { ThemeSurface } from "./ThemeSurface";

// Settings IA (ADR 0048, ratified 2026-06-28). ONE surface, organized by DOMAIN — what a
// setting *does* — not by scope. Scope (host vs agent) is a per-field inheritance badge
// (ADR 0047), never a nav axis. The sidenav splits into labeled groups:
//
//   Agent        — what defines the focused agent: Identity · Model · Behavior · Knowledge ·
//                  Integrations (Plugins). Schema-driven domains carry the ADR 0047 badge.
//   Capabilities — what the agent is wired to: Tools · MCP · Skills · Subagents · Delegates,
//                  plus the Sharing & tiers knobs.
//   Box          — box-wide ops (HOST CONSOLE ONLY): Overview · Fleet · Telemetry · Box config.
//   This console — device-local prefs (NOT agent config, no cascade): Theme · Chat · Keyboard.

type Section = { id: string; label: string; icon: LucideIcon; render: () => ReactNode };

// The Plugins manager (install · enable · configure, plus the Discover directory) — the
// Integrations domain. Per-plugin config is inline per row (ADR 0059).
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

// Identity domain: the bespoke Identity panel (name + persona/SOUL via /api/config) followed
// by the schema-driven identity/operator/access fields (operator · org · project dir · A2A token).
function IdentityHome() {
  return (
    <>
      <IdentityPanel />
      <SettingsCategoryPanel category="Identity" title="Operator & access" />
    </>
  );
}

// AGENT — what defines the focused agent (schema domains + the bespoke Identity panel).
const AGENT_SECTIONS: Section[] = [
  { id: "identity", label: "Identity", icon: Sparkles, render: () => <IdentityHome /> },
  // id stays "model" (the former "settings"/"Model & Routing"). It now renders ONLY the Model
  // domain (model · routing · caching) instead of the whole Agent category (ADR 0048 C4).
  { id: "model", label: "Model", icon: Cpu, render: () => <SettingsCategoryPanel category="Model" title="Model & routing" /> },
  { id: "behavior", label: "Behavior", icon: Brain, render: () => <SettingsCategoryPanel category="Behavior" title="Behavior" /> },
  { id: "knowledge", label: "Knowledge", icon: Database, render: () => <SettingsCategoryPanel category="Knowledge" title="Knowledge" /> },
  { id: "plugins", label: "Integrations", icon: Puzzle, render: () => <PluginSettingsHome /> },
];

// CAPABILITIES — what the agent is wired to (rich bespoke managers) + the sharing/tier knobs.
const CAPABILITY_SECTIONS: Section[] = [
  { id: "tools", label: "Tools", icon: Wrench, render: () => <ToolsPanel /> },
  { id: "mcp", label: "MCP", icon: Plug, render: () => <McpPanel /> },
  { id: "skills", label: "Skills", icon: BookMarked, render: () => <PlaybooksSurface /> },
  { id: "subagents", label: "Subagents", icon: Bot, render: () => <SubagentsPanel /> },
  { id: "delegates", label: "Delegates", icon: Network, render: () => <DelegatesSection /> },
  { id: "sharing", label: "Sharing & tiers", icon: Share2, render: () => <SettingsCategoryPanel category="Capabilities" title="Sharing & tiers" emptyHint="No sharing tiers to configure." /> },
];

// BOX — box-wide operations (host console only). Overview/Fleet/Telemetry are bespoke surfaces;
// "Box config" is the schema home for the host box-runtime + telemetry knobs (ADR 0047 D8).
const BOX_SECTIONS: Section[] = [
  { id: "overview", label: "Overview", icon: Gauge, render: () => <OverviewPanel /> },
  { id: "fleet", label: "Fleet", icon: Server, render: () => <FleetSurface /> },
  { id: "telemetry", label: "Telemetry", icon: BarChart3, render: () => <TelemetrySurface /> },
  { id: "boxconfig", label: "Box config", icon: SlidersHorizontal, render: () => <SettingsCategoryPanel category="Box" title="Box config" emptyHint="No box-wide config on this box yet." /> },
];

// THIS CONSOLE — device-local preferences. These don't cascade and use their own backends
// (Theme → /api/theme; Chat/Keyboard → the persisted UI store). Kept visibly separate from
// agent config so the "this device vs this agent" line is obvious (ADR 0048 §2.4).
const CONSOLE_SECTIONS: Section[] = [
  { id: "theme", label: "Theme", icon: Palette, render: () => <ThemeSurface /> },
  { id: "chat", label: "Chat", icon: MessageSquare, render: () => <ChatSettingsPanel /> },
  { id: "keybindings", label: "Keyboard", icon: Keyboard, render: () => <KeybindingsPanel /> },
];

// One consolidated settings surface. `initialSection` deep-links a section (the overlay / a ⌘K
// command). The Box group is gated to the host console.
export function SettingsSurface({ initialSection }: { only?: "host" | "workspace"; initialSection?: string } = {}) {
  const onHost = isHostConsole();
  const persistedSection = useUI((s) => s.settingsSection);
  const setSection = useUI((s) => s.setSettingsSection);

  // Deep-link: select the requested section once when opened on one (overlay / palette).
  useEffect(() => {
    if (initialSection) setSection(initialSection);
  }, [initialSection, setSection]);

  const sections = [
    ...AGENT_SECTIONS,
    ...CAPABILITY_SECTIONS,
    ...(onHost ? BOX_SECTIONS : []),
    ...CONSOLE_SECTIONS,
  ];
  const active = sections.find((s) => s.id === persistedSection) ?? sections[0];
  const toItem = (s: Section) => ({ id: s.id, label: s.label, icon: <s.icon size={15} /> });
  const groups = [
    { label: "Agent", items: AGENT_SECTIONS.map(toItem) },
    { label: "Capabilities", items: CAPABILITY_SECTIONS.map(toItem) },
    ...(onHost ? [{ label: "Box", items: BOX_SECTIONS.map(toItem) }] : []),
    { label: "This console", items: CONSOLE_SECTIONS.map(toItem) },
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
