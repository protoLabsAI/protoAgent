import { BarChart3, Bot, BookMarked, Boxes, Brain, Cpu, Database, FlaskConical, Gauge, Keyboard, KeyRound, Lock, MessageSquare, Network, Package, Palette, Plug, Puzzle, Server, Share2, Smartphone, Sparkles, Store, Wrench } from "lucide-react";
import { useFlagPredicate } from "../flags/flags";
import { visibleSections } from "./sectionGate";
import type { LucideIcon } from "lucide-react";
import { useEffect, type ReactNode } from "react";

import { SideNav, Tabs } from "@protolabsai/ui/navigation";
import { StatusDot } from "@protolabsai/ui/data";
import { useQuery } from "@tanstack/react-query";

import { IdentityPanel } from "../agent/IdentityPanel";
import { pythonRuntimeQuery } from "../lib/queries";
import { pythonRuntimeView } from "../app/pythonRuntime";
import { McpPanel } from "../app/McpPanel";
import { SubagentsPanel } from "../app/SubagentsPanel";
import { ToolsPanel } from "../app/ToolsPanel";
import { isHostConsole } from "../lib/api";
import { PluginsSurface } from "../plugins/PluginsSurface";
import { PlaybooksSurface } from "../playbooks/PlaybooksSurface";
import { TelemetrySurface } from "../telemetry/TelemetrySurface";
import { useIsMobile } from "../lib/useIsMobile";
import { useUI } from "../state/uiStore";
import { DelegatesSection } from "./DelegatesSection";
import { SnapshotPanel } from "./SnapshotPanel";
import { FleetSurface } from "./FleetSurface";
import { KeybindingsPanel } from "./KeybindingsPanel";
import { ChatSettingsPanel } from "./ChatSettingsPanel";
import { DeveloperPanel } from "./DeveloperPanel";
import { developerPanelVisible, useDeveloperChannel } from "../flags/flags";
import { OverviewPanel } from "./OverviewPanel";
import { DevicesPanel } from "./DevicesPanel";
import { SecretsPanel } from "./SecretsPanel";
import { PublishedLinksSection } from "./PublishedLinksSection";
import { OAuthAccountSection } from "./OAuthAccountSection";
import { SettingsCategoryPanel } from "./SettingsCategory";
import { ThemeSurface } from "./ThemeSurface";

// Settings IA (ADR 0048, ratified 2026-06-28). ONE surface, organized by DOMAIN — what a
// setting *does* — not by scope. Scope (host vs agent) is a per-field inheritance badge
// (ADR 0047), never a nav axis. The sidenav splits into labeled groups:
//
//   Agent        — what defines the focused agent: Identity · Model · Behavior · Knowledge ·
//                  Plugins. Schema-driven domains carry the ADR 0047 badge.
//   Capabilities — what the agent is wired to: Tools · MCP · Skills · Subagents · Delegates.
//                  Each manager owns its sharing/tier knob via a contextual chip (no extra panel).
//   Box          — box-wide ops: Overview · Fleet · Telemetry. Overview + Telemetry are host
//                  console only; Fleet renders in every sister agent's window too (it names
//                  the hub's fleet from anywhere). Box-runtime + telemetry knobs are chips on
//                  Fleet / Telemetry, not a separate empty panel.
//   This console — device-local prefs (NOT agent config, no cascade): Theme · Chat · Keyboard.

type Section = {
  id: string;
  label: string;
  icon: LucideIcon;
  render: () => ReactNode;
  /** Developer flag gating this section (ADR 0068). Absent = always shown. A flag-off
   *  section is filtered from BOTH the nav and the resolvable set, so a persisted id
   *  pointing at it falls back to the first visible section rather than a blank pane. */
  flag?: string;
  /** Box sections only: this one reads the FOCUSED agent's endpoints, so it renders on the
   *  host console alone. Absent on a Box section = it names the whole box from any window
   *  (Fleet — `/api/fleet` is a hub path), so a sister agent's console gets it too. */
  hostOnly?: boolean;
};

// The Plugins manager (install · enable · configure, plus the Discover directory) — the
// Plugins domain. Per-plugin config is inline per row (ADR 0059).
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

// AGENT — what defines the focused agent (schema domains + the bespoke Identity panel).
const AGENT_SECTIONS: Section[] = [
  // Identity is the bespoke panel ONLY (name + persona/SOUL via /api/config) so the SOUL editor
  // fills the panel. The operator/org/access schema fields are their own one-click section (a
  // chip-in-a-dialog was unnecessary extra clicking).
  { id: "identity", label: "Identity", icon: Sparkles, render: () => <IdentityPanel /> },
  { id: "access", label: "Operator & access", icon: KeyRound, render: () => <SettingsCategoryPanel category="Identity" title="Operator & access" /> },
  // Paired devices (ADR 0087) — sits next to access because it IS access: each device holds
  // its own revocable token rather than sharing the operator bearer.
  // Behind `settings.devices` (ADR 0068), default OFF — see the flag's description in
  // runtime/flags.py. The pairing flow stopped the desktop app from starting four times; it
  // stays hidden until the whole path is exercised in the desktop app itself.
  { id: "devices", label: "Devices", icon: Smartphone, flag: "settings.devices", render: () => <DevicesPanel /> },
  // id stays "model" (the former "settings"/"Model & Routing"). It now renders ONLY the Model
  // domain (model · routing · caching) instead of the whole Agent category (ADR 0048 C4).
  // The OAuth account lifecycle (#2460) renders under the schema-driven Model form —
  // status/Disconnect/Reconnect were wizard-only before, a recovery dead end post-setup.
  { id: "model", label: "Model", icon: Cpu, render: () => (
    <SettingsCategoryPanel category="Model" title="Model & routing" lead={<OAuthAccountSection />} />
  ) },
  { id: "behavior", label: "Behavior", icon: Brain, render: () => <SettingsCategoryPanel category="Behavior" title="Behavior" /> },
  { id: "knowledge", label: "Knowledge", icon: Database, render: () => <SettingsCategoryPanel category="Knowledge" title="Knowledge" /> },
  // External secrets manager (ADR 0080) — schema fields + the status/test/sync card.
  // Behind `secrets-panel` (ADR 0068), dev channel only — see the flag in runtime/flags.py.
  // Flag-off: `shown()` drops it from the nav AND from id resolution, so a persisted "secrets"
  // id falls back to the first visible section instead of a blank pane.
  { id: "secrets", label: "Secrets", icon: Lock, flag: "secrets-panel", render: () => <SecretsPanel /> },
  // Publishing a chat thread to the hosted viewer (#2179 P2, #2682-#2684) — schema fields
  // (endpoint URLs) + the published-links list/revoke card, same footer-seam pattern as
  // Secrets above. Behind `chat.publish` (ADR 0068), off by default: the hosted service
  // (#2685) doesn't exist yet, so there's nothing for this panel to manage until an
  // operator has a real endpoint to configure.
  { id: "publish", label: "Publish", icon: Share2, flag: "chat.publish", render: () => (
    <SettingsCategoryPanel category="Publish" title="Publish" footer={<PublishedLinksSection />} />
  ) },
  { id: "plugins", label: "Plugins", icon: Puzzle, render: () => <PluginSettingsHome /> },
  // Last in the group on purpose: a snapshot exports what every section above configures
  // (identity, model, behavior, plugins) — it IS this agent's definition (ADR 0091).
  { id: "snapshot", label: "Snapshot", icon: Package, render: () => <SnapshotPanel /> },
];

// CAPABILITIES — what the agent is wired to (rich bespoke managers). Each manager owns its own
// sharing/tier knob via a contextual "…sharing" chip in its header (Skills/MCP) — not a separate
// schema-only panel (ADR 0048 §2.2: a chip is a shortcut to the canonical field, same save path).
const CAPABILITY_SECTIONS: Section[] = [
  { id: "tools", label: "Tools", icon: Wrench, render: () => <ToolsPanel /> },
  { id: "mcp", label: "MCP", icon: Plug, render: () => <McpPanel /> },
  { id: "skills", label: "Skills", icon: BookMarked, render: () => <PlaybooksSurface /> },
  { id: "subagents", label: "Subagents", icon: Bot, render: () => <SubagentsPanel /> },
  { id: "delegates", label: "Delegates", icon: Network, render: () => <DelegatesSection /> },
];

// BOX — box-wide operations. Overview + Telemetry read the FOCUSED agent's endpoints
// (/api/runtime, /api/telemetry), so they'd mean something different in a sister agent's
// window and stay host-console-only. Fleet is the exception: `/api/fleet` is a hub path
// (never slug-scoped), so it names the SAME fleet from every window — every sister agent
// manages the roster its hub does. The host box-runtime + telemetry knobs are reached via
// chips on Fleet ("Box runtime") and Telemetry, not a separate empty schema panel.
const BOX_SECTIONS: Section[] = [
  { id: "overview", label: "Overview", icon: Gauge, hostOnly: true, render: () => <OverviewPanel /> },
  { id: "fleet", label: "Fleet", icon: Server, render: () => <FleetSurface /> },
  { id: "telemetry", label: "Telemetry", icon: BarChart3, hostOnly: true, render: () => <TelemetrySurface /> },
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
// command). The Box group's agent-scoped sections are gated to the host console; Fleet is not.
export function SettingsSurface({ initialSection }: { only?: "host" | "workspace"; initialSection?: string } = {}) {
  const onHost = isHostConsole();
  // On phones the two-column shell can't fit a 200px rail + readable content, so collapse
  // the SideNav to its DS <select> (mobile only — the desktop rail is deliberately a tablist).
  const isMobile = useIsMobile();
  const persistedSection = useUI((s) => s.settingsSection);
  const setSection = useUI((s) => s.setSettingsSection);

  // Deep-link: select the requested section once when opened on one (overlay / palette).
  useEffect(() => {
    if (initialSection) setSection(initialSection);
  }, [initialSection, setSection]);

  // The Developer panel (ADR 0068) joins "This console" only off prod — a dev build, a
  // non-prod channel, or an explicit ?dev/?flag: reveal — so production operators never see it.
  const channel = useDeveloperChannel();
  const consoleSections: Section[] = developerPanelVisible(channel)
    ? [...CONSOLE_SECTIONS, { id: "developer", label: "Developer", icon: FlaskConical, render: () => <DeveloperPanel /> }]
    : CONSOLE_SECTIONS;

  // Drop flag-off sections everywhere they'd be reachable — nav, active-id resolution, and
  // the ⌘K/deep-link path that reads the same persisted id — and the same for `hostOnly`
  // sections off the host console. A sister agent's window keeps
  // the Box group, narrowed to what still names the box from there (Fleet). Narrowing the group
  // — rather than dropping it — is what makes the header's "Fleet settings" deep-link resolve
  // in a member window instead of silently falling back to the first section.
  const flagOn = useFlagPredicate();
  const shown = (list: Section[]) => visibleSections(list, flagOn, onHost);
  const agentSections = shown(AGENT_SECTIONS);
  const capabilitySections = shown(CAPABILITY_SECTIONS);
  const boxSections = shown(BOX_SECTIONS);

  const sections = [
    ...agentSections,
    ...capabilitySections,
    ...boxSections,
    ...consoleSections,
  ];
  const active = sections.find((s) => s.id === persistedSection) ?? sections[0];

  // #2186 — the managed Python runtime's state was computed for the Tools panel's
  // install card, but nothing ADVERTISED it before a tool call failed: on a stock
  // desktop install you learned the runtime was unprovisioned by tripping over it
  // mid-task. Badge the Tools nav entry whenever the card is actionable
  // (unprovisioned / stale baseline / failed install) so the state is met while
  // browsing, not at failure time — the deps_missing-badge pattern (ADR 0094 D4).
  const pyRuntime = pythonRuntimeView(useQuery(pythonRuntimeQuery()).data);
  const toolsBadge =
    pyRuntime.kind === "action" ? (
      <span
        title={
          pyRuntime.installing
            ? "Installing Python runtime…"
            : pyRuntime.error
              ? "Python runtime install failed — retry in Tools"
              : pyRuntime.stale
                ? "Python runtime needs a refresh"
                : "Python runtime not provisioned — execute_code and the document skills can't run yet"
        }
      >
        <StatusDot status="warning" pulse={pyRuntime.installing} />
      </span>
    ) : undefined;

  const toItem = (s: Section) => ({
    id: s.id,
    label: s.label,
    icon: <s.icon size={15} />,
    badge: s.id === "tools" ? toolsBadge : undefined,
  });
  const groups = [
    { label: "Agent", items: agentSections.map(toItem) },
    { label: "Capabilities", items: capabilitySections.map(toItem) },
    ...(boxSections.length ? [{ label: "Box", items: boxSections.map(toItem) }] : []),
    { label: "This console", items: consoleSections.map(toItem) },
  ];

  return (
    <div className="settings-shell">
      <SideNav responsive={isMobile} ariaLabel="Settings sections" groups={groups} active={active.id} onSelect={(id) => setSection(id)} />
      <div className="settings-content">
        {active.render()}
      </div>
    </div>
  );
}
