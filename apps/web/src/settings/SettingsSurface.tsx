import { Activity, BarChart3, Bot, BookMarked, Boxes, Brain, Cpu, Database, FlaskConical, Gauge, Keyboard, KeyRound, Lock, MessageSquare, Network, Package, Palette, Plug, Puzzle, Server, Share2, Smartphone, Sparkles, Store, Wrench } from "lucide-react";
import { useFlagPredicate } from "../flags/flags";
import { settingsSectionGroups } from "./sections";
import type { LucideIcon } from "lucide-react";
import type { SectionMeta, SettingsSectionIcon, SettingsSectionId } from "./sections";
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
import { ProvidersPanel } from "./ProvidersPanel";
import { SettingsCategoryPanel } from "./SettingsCategory";
import { ThemeSurface } from "./ThemeSurface";

// The Settings IA and the section TABLE (ids · labels · icon names · flag/hostOnly gates ·
// group order) live in ./sections — a leaf that imports neither React nor lucide, so ⌘K and
// the desktop Launcher can name a section without dragging this whole panel tree along.
// This file supplies the two halves that genuinely need React: what each id RENDERS, and the
// name → lucide component map.

// Explicit, statically-imported icon components rather than lib/lucideIcon's lazy resolver:
// the rail is on screen the instant Settings opens and must not flicker through a Suspense
// fallback, and a static map keeps the 23 glyphs tree-shakeable instead of pulling the whole
// lucide set. `Record<SettingsSectionIcon, …>` makes the map exhaustive — a new section with
// an unmapped icon name is a type error, not a missing glyph.
const ICONS: Record<SettingsSectionIcon, LucideIcon> = {
  Sparkles,
  KeyRound,
  Smartphone,
  Cpu,
  Brain,
  Database,
  Activity,
  Lock,
  Share2,
  Puzzle,
  Package,
  Wrench,
  Plug,
  BookMarked,
  Bot,
  Network,
  Gauge,
  Server,
  BarChart3,
  Palette,
  MessageSquare,
  Keyboard,
  FlaskConical,
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

// What each section id RENDERS. Keyed by id and typed `Record<SettingsSectionId, …>`, so
// adding a row to the table without a panel here fails the build rather than blanking a pane.
const RENDERERS: Record<SettingsSectionId, () => ReactNode> = {
  identity: () => <IdentityPanel />,
  access: () => <SettingsCategoryPanel category="Identity" title="Operator & access" />,
  devices: () => <DevicesPanel />,
  // Connections is the first, default-open accordion group (the OAuth account lifecycle, #2460).
  model: () => (
    <SettingsCategoryPanel
      category="Model"
      title="Model & routing"
      leadTitle="Connections"
      lead={<ProvidersPanel />}
    />
  ),
  behavior: () => <SettingsCategoryPanel category="Behavior" title="Behavior" />,
  knowledge: () => <SettingsCategoryPanel category="Knowledge" title="Knowledge" />,
  // graph/settings_schema.py maps section "Tracing" → category "Observability"; naming any
  // other category here renders an empty panel (#3017).
  tracing: () => <SettingsCategoryPanel category="Observability" title="Tracing" />,
  secrets: () => <SecretsPanel />,
  publish: () => <SettingsCategoryPanel category="Publish" title="Publish" footer={<PublishedLinksSection />} />,
  plugins: () => <PluginSettingsHome />,
  snapshot: () => <SnapshotPanel />,
  tools: () => <ToolsPanel />,
  mcp: () => <McpPanel />,
  skills: () => <PlaybooksSurface />,
  subagents: () => <SubagentsPanel />,
  delegates: () => <DelegatesSection />,
  overview: () => <OverviewPanel />,
  fleet: () => <FleetSurface />,
  telemetry: () => <TelemetrySurface />,
  theme: () => <ThemeSurface />,
  chat: () => <ChatSettingsPanel />,
  keybindings: () => <KeybindingsPanel />,
  developer: () => <DeveloperPanel />,
};

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
  // settingsSectionGroups drops flag-off sections everywhere they'd be reachable — nav,
  // active-id resolution, and the ⌘K/deep-link path that reads the same persisted id — and
  // the same for `hostOnly` sections off the host console. A sister agent's window keeps the
  // Box group, narrowed to what still names the box from there (Fleet). Narrowing the group —
  // rather than dropping it — is what makes the header's "Fleet settings" deep-link resolve in
  // a member window instead of silently falling back to the first section.
  const flagOn = useFlagPredicate();
  const groups = settingsSectionGroups({ flagOn, onHost, developerVisible: developerPanelVisible(channel) });

  // The resolvable set IS the flattened nav, so nothing can be reachable that isn't listed.
  const sections = groups.flatMap((g) => g.sections);
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

  const toItem = (s: SectionMeta) => {
    const Icon = ICONS[s.icon as SettingsSectionIcon];
    return {
      id: s.id,
      label: s.label,
      icon: <Icon size={15} />,
      badge: s.id === "tools" ? toolsBadge : undefined,
    };
  };
  const navGroups = groups.map((g) => ({ label: g.label, items: g.sections.map(toItem) }));

  return (
    <div className="settings-shell">
      <SideNav responsive={isMobile} ariaLabel="Settings sections" groups={navGroups} active={active.id} onSelect={(id) => setSection(id)} />
      <div className="settings-content">
        {RENDERERS[active.id as SettingsSectionId]()}
      </div>
    </div>
  );
}
