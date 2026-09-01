import { visibleSections } from "./sectionGate";
import type { GatedSection } from "./sectionGate";

// The Settings section TABLE — ids, labels, icon names, gates, group order — and nothing else.
//
// This module is a LEAF ON PURPOSE. Its only import is ./sectionGate, which itself imports
// nothing; it pulls in no React, no lucide components and no panels. That is what lets a
// consumer that merely needs to NAME a settings section (⌘K, a deep link, the desktop
// Launcher window, which mounts the palette registry but never mounts App) read the table
// without welding the whole settings tree onto its path: importing anything at all from
// SettingsSurface.tsx eagerly drags ~90 modules / ~800 KB of panel source with it, and CI has
// no bundle-size gate that would catch the regression. sections.test.ts pins the rule.
//
// SettingsSurface.tsx COMPOSES this table with the halves that genuinely need React: the
// `render: () => …` per id and the name → lucide component map.
//
// Settings IA (ADR 0048, ratified 2026-06-28). ONE surface, organized by DOMAIN — what a
// setting *does* — not by scope. Scope (host vs agent) is a per-field inheritance badge
// (ADR 0047), never a nav axis. The sidenav splits into labeled groups:
//
//   Agent        — what defines the focused agent: Identity · Model · Behavior · Knowledge ·
//                  Tracing · Plugins. Schema-driven domains carry the ADR 0047 badge.
//   Capabilities — what the agent is wired to: Tools · MCP · Skills · Subagents · Delegates.
//                  Each manager owns its sharing/tier knob via a contextual chip (no extra panel).
//   Box          — box-wide ops: Overview · Fleet · Telemetry. Overview + Telemetry are host
//                  console only; Fleet renders in every sister agent's window too (it names
//                  the hub's fleet from anywhere). Box-runtime + telemetry knobs are chips on
//                  Fleet / Telemetry, not a separate empty panel.
//   This console — device-local prefs (NOT agent config, no cascade): Theme · Chat · Keyboard.

export type SectionMeta = GatedSection & {
  /** Nav label. Stable — the persisted `settingsSection` in uiStore keys off `id`, not this. */
  label: string;
  /**
   * The lucide icon's CANONICAL name, never the component: a component here would drag
   * lucide-react into every consumer of this table. It must be a key of lucide's `icons` map,
   * because the table has two consumers and only one of them resolves names the lax way —
   * SettingsSurface maps the name to a statically-imported component (the rail must not
   * flicker through a Suspense fallback), while a consumer that can tolerate a lazy glyph
   * feeds the same name to lib/lucideIcon, which looks it up in `icons` and falls back to
   * Package on a miss. lucide keeps DEPRECATED aliases (`BarChart3`) as top-level named
   * exports but drops them from `icons`, so an alias here type-checks, renders correctly in
   * the rail, and silently degrades to the Package box in ⌘K / the Launcher. sections.test.ts
   * pins every name against `icons` so that gap can't reopen.
   */
  icon: string;
};

// AGENT — what defines the focused agent (schema domains + the bespoke Identity panel).
export const AGENT_SECTIONS = [
  // Identity is the bespoke panel ONLY (name + persona/SOUL via /api/config) so the SOUL editor
  // fills the panel. The operator/org/access schema fields are their own one-click section (a
  // chip-in-a-dialog was unnecessary extra clicking).
  { id: "identity", label: "Identity", icon: "Sparkles" },
  { id: "access", label: "Operator & access", icon: "KeyRound" },
  // Paired devices (ADR 0087) — sits next to access because it IS access: each device holds
  // its own revocable token rather than sharing the operator bearer.
  // Behind `settings.devices` (ADR 0068), default OFF — see the flag's description in
  // runtime/flags.py. The pairing flow stopped the desktop app from starting four times; it
  // stays hidden until the whole path is exercised in the desktop app itself.
  { id: "devices", label: "Devices", icon: "Smartphone", flag: "settings.devices" },
  // id stays "model" (the former "settings"/"Model & Routing"). It now renders ONLY the Model
  // domain (model · routing · caching) instead of the whole Agent category (ADR 0048 C4).
  // Connections is the first, default-open accordion group. The OAuth account lifecycle
  // (#2460) lives there beside gateway connections instead of outside the accordion.
  { id: "model", label: "Model", icon: "Cpu" },
  { id: "behavior", label: "Behavior", icon: "Brain" },
  { id: "knowledge", label: "Knowledge", icon: "Database" },
  // Langfuse tracing (#3017) — ADR 0006's deep-trace half. It sits in the AGENT group rather
  // than beside Box ▸ Telemetry because its four fields are AGENT-scoped credentials and Box ▸
  // Telemetry is `hostOnly`: a fleet member launched by the desktop app (`--ui none`) serves no
  // console of its own and is only ever seen through a member window, which is precisely where
  // tracing had no reachable switch. Telemetry keeps a QuickSetting chip on the same four keys
  // for the host console, so both halves of ADR 0006 still meet in one place there.
  { id: "tracing", label: "Tracing", icon: "Activity" },
  // External secrets manager (ADR 0080) — schema fields + the status/test/sync card.
  // Behind `secrets-panel` (ADR 0068), dev channel only — see the flag in runtime/flags.py.
  // Flag-off: `shown()` drops it from the nav AND from id resolution, so a persisted "secrets"
  // id falls back to the first visible section instead of a blank pane.
  { id: "secrets", label: "Secrets", icon: "Lock", flag: "secrets-panel" },
  // Publishing a chat thread to the hosted viewer (#2179 P2, #2682-#2684) — schema fields
  // (endpoint URLs) + the published-links list/revoke card, same footer-seam pattern as
  // Secrets above. Behind `chat.publish` (ADR 0068), off by default: the hosted service
  // (#2685) doesn't exist yet, so there's nothing for this panel to manage until an
  // operator has a real endpoint to configure.
  { id: "publish", label: "Publish", icon: "Share2", flag: "chat.publish" },
  { id: "plugins", label: "Plugins", icon: "Puzzle" },
  // Last in the group on purpose: a snapshot exports what every section above configures
  // (identity, model, behavior, plugins) — it IS this agent's definition (ADR 0091).
  { id: "snapshot", label: "Snapshot", icon: "Package" },
] as const satisfies readonly SectionMeta[];

// CAPABILITIES — what the agent is wired to (rich bespoke managers). Each manager owns its own
// sharing/tier knob via a contextual "…sharing" chip in its header (Skills/MCP) — not a separate
// schema-only panel (ADR 0048 §2.2: a chip is a shortcut to the canonical field, same save path).
export const CAPABILITY_SECTIONS = [
  { id: "tools", label: "Tools", icon: "Wrench" },
  { id: "mcp", label: "MCP", icon: "Plug" },
  { id: "skills", label: "Skills", icon: "BookMarked" },
  { id: "subagents", label: "Subagents", icon: "Bot" },
  { id: "delegates", label: "Delegates", icon: "Network" },
] as const satisfies readonly SectionMeta[];

// BOX — box-wide operations. Overview + Telemetry read the FOCUSED agent's endpoints
// (/api/runtime, /api/telemetry), so they'd mean something different in a sister agent's
// window and stay host-console-only. Fleet is the exception: `/api/fleet` is a hub path
// (never slug-scoped), so it names the SAME fleet from every window — every sister agent
// manages the roster its hub does. The host box-runtime + telemetry knobs are reached via
// chips on Fleet ("Box runtime") and Telemetry, not a separate empty schema panel.
export const BOX_SECTIONS = [
  { id: "overview", label: "Overview", icon: "Gauge", hostOnly: true },
  { id: "fleet", label: "Fleet", icon: "Server" },
  { id: "telemetry", label: "Telemetry", icon: "ChartColumn", hostOnly: true },
] as const satisfies readonly SectionMeta[];

// THIS CONSOLE — device-local preferences. These don't cascade and use their own backends
// (Theme → /api/theme; Chat/Keyboard → the persisted UI store). Kept visibly separate from
// agent config so the "this device vs this agent" line is obvious (ADR 0048 §2.4).
export const CONSOLE_SECTIONS = [
  { id: "theme", label: "Theme", icon: "Palette" },
  { id: "chat", label: "Chat", icon: "MessageSquare" },
  { id: "keybindings", label: "Keyboard", icon: "Keyboard" },
] as const satisfies readonly SectionMeta[];

// The Developer panel (ADR 0068) joins "This console" only off prod — a dev build, a
// non-prod channel, or an explicit ?dev/?flag: reveal — so production operators never see it.
// It is appended at render time rather than carrying a `flag:` because its visibility is a
// CHANNEL decision (developerPanelVisible), not a per-flag one.
export const DEVELOPER_SECTION = {
  id: "developer",
  label: "Developer",
  icon: "FlaskConical",
} as const satisfies SectionMeta;

export const GROUP_LABELS = {
  agent: "Agent",
  capabilities: "Capabilities",
  box: "Box",
  console: "This console",
} as const;

/** The four nav headings, as a literal union — a `.label ===` match can't be fat-fingered. */
export type SettingsGroupLabel = (typeof GROUP_LABELS)[keyof typeof GROUP_LABELS];

/** Every declared section, gates ignored — the domain of the id/icon unions below. */
export const ALL_SECTIONS = [
  ...AGENT_SECTIONS,
  ...CAPABILITY_SECTIONS,
  ...BOX_SECTIONS,
  ...CONSOLE_SECTIONS,
  DEVELOPER_SECTION,
] as const;

/** The persisted `settingsSection` ids (uiStore). Changing one is a migration, not a rename. */
export type SettingsSectionId = (typeof ALL_SECTIONS)[number]["id"];
/** The lucide names the table actually uses — keeps SettingsSurface's icon map exhaustive. */
export type SettingsSectionIcon = (typeof ALL_SECTIONS)[number]["icon"];
/**
 * A row of the table with its literal `id`/`icon` INTACT — deliberately not widened to
 * SectionMeta. Widening is what would force an `as SettingsSectionId` at every lookup, and a
 * cast is exactly where `Record<SettingsSectionId, …>` stops being a guarantee: it is checked
 * where the map is declared but not where it is read. Keeping the literals means a row that
 * reached `settingsSectionGroups` without reaching ALL_SECTIONS (two hand-kept compositions of
 * the same arrays) fails to type-check here, instead of rendering a blank pane and no glyph.
 */
export type SettingsSection = (typeof ALL_SECTIONS)[number];

export type SectionGroup = { label: SettingsGroupLabel; sections: SettingsSection[] };

export type SectionVisibility = {
  /** ADR 0068 flag predicate — useFlagPredicate() in the console. */
  flagOn: (id: string) => boolean;
  /**
   * isHostConsole(); false in a sister agent's window. REQUIRED, unlike `developerVisible`:
   * there is no safe default for it. Defaulting it to `true` (as this once did) hands a
   * consumer that forgot it the host console's answer, so a member window would offer
   * Overview/Telemetry deep links that then resolve to the first section instead — the exact
   * silent fallback quirk 2 below exists to prevent. Omitting `developerVisible` only ever
   * offers LESS, so that one keeps its default.
   */
  onHost: boolean;
  /** developerPanelVisible(channel) — appends the Developer section to "This console". */
  developerVisible?: boolean;
};

/**
 * The nav groups, in render order, with both quirks the surface has always had:
 *
 *  1. "This console" is NOT run through the gate. Its sections are device-local prefs with no
 *     flag and no host axis; instead the Developer section is APPENDED here when the channel
 *     allows it. A consumer that ran this group through `visibleSections` too would be
 *     mirroring a rule that does not exist.
 *  2. The Box group is omitted only when it is EMPTY. Off the host console it NARROWS to
 *     Fleet rather than disappearing — that is what keeps openGlobalSettings("fleet")
 *     resolving in a member window instead of falling back to the first section.
 */
export function settingsSectionGroups({
  flagOn,
  onHost,
  developerVisible = false,
}: SectionVisibility): SectionGroup[] {
  // Generic, not `(list: readonly SectionMeta[])`: the annotation would widen every row to
  // SectionMeta here and lose the literal ids/icons the consumer's exhaustive maps key off.
  const shown = <T extends SectionMeta>(list: readonly T[]) => visibleSections(list, flagOn, onHost);
  const agentSections = shown(AGENT_SECTIONS);
  const capabilitySections = shown(CAPABILITY_SECTIONS);
  const boxSections = shown(BOX_SECTIONS);
  const consoleSections: SettingsSection[] = developerVisible
    ? [...CONSOLE_SECTIONS, DEVELOPER_SECTION]
    : [...CONSOLE_SECTIONS];
  return [
    { label: GROUP_LABELS.agent, sections: agentSections },
    { label: GROUP_LABELS.capabilities, sections: capabilitySections },
    ...(boxSections.length ? [{ label: GROUP_LABELS.box, sections: boxSections }] : []),
    { label: GROUP_LABELS.console, sections: consoleSections },
  ];
}

/**
 * The flat, ordered resolvable set — the same list a persisted/deep-linked id resolves
 * against. Flattening the groups (rather than re-deriving) is what keeps nav and resolution
 * from drifting apart: a section that is not in some group is not reachable, full stop.
 */
export function settingsSections(visibility: SectionVisibility): SettingsSection[] {
  return settingsSectionGroups(visibility).flatMap((g) => g.sections);
}
