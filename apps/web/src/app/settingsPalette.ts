// Every Settings section as a ⌘K deep-link, GENERATED from the section table.
//
// Before this module the palette hand-wrote three Settings rows — a bare "Settings", plus
// "Settings: Fleet" and "Settings: Telemetry" — and the other twenty sections (Theme,
// Keyboard, Model, Tools, MCP, Skills, Subagents, Delegates, Secrets, Snapshot, …) were
// palette-invisible. Writing twenty more rows by hand is not the fix, because hand-writing
// them is the failure: three rows is a list somebody forgot to extend, and nothing failed
// when they did. Deriving the rows from `settings/sections.ts` makes the palette's coverage
// a consequence of the table rather than a parallel list, so a NEW section is deep-linkable
// the moment it is declared — and `SECTION_KEYWORDS` below is exhaustive over
// `SettingsSectionId`, so adding one without search vocabulary is a type error rather than
// a silently unfindable row.
//
// ── Why this module imports the LEAF and not the surface ────────────────────────────
// `settings/sections.ts` is import-light on purpose: no React, no lucide components, no
// panels. Reaching for `settings/SettingsSurface` to get the same ids would weld ~69
// modules / ~470 KB of eager panel code onto the ⌘K path AND onto the desktop Launcher
// window, which mounts the palette registry but never mounts App — and CI has no
// bundle-size gate that would notice. `settingsPalette.test.ts` parses this file's own
// import list to keep that door shut.
//
// ── Gating is DECLARATIVE — this module filters nothing ─────────────────────────────
// A row for a flag-gated section carries the leaf's `flag` verbatim; a host-only section
// carries `hostOnly`. Both are DATA the host resolves per render
// (`visiblePaletteCommands(flagOn, isHostConsole())`, ADR 0061), which is the whole point:
// `useFlagPredicate` fails CLOSED while `/api/flags` is in flight, so a gate evaluated HERE
// — at module load, before the request has even been made — would hide Secrets, Devices and
// Publish forever, on every channel, in a way no test that stubs the flags would reproduce.
//
// That per-render gate is also what keeps a gated row from POISONING state. A deep-link
// applies its section id through `openGlobalSettings` → `SettingsSurface`'s
// `initialSection` effect, which writes the id into the PERSISTED `settingsSection`
// (uiStore is not partialized) BEFORE the section gate runs, and only then falls back to
// the first visible section. A row that could be RUN while its section is gated off would
// therefore leave a dead id in localStorage — a settings dialog that opens on Identity
// forever after, for no visible reason. Because `flag`/`hostOnly` ride the row, the host
// never renders it in that state, so the run can't happen.
import { lucideIcon } from "../lib/lucideIcon";
import { settingsSectionGroups } from "../settings/sections";
import type { PaletteCommand } from "../ext/paletteRegistry";
import type { SectionMeta, SettingsSectionId } from "../settings/sections";

/**
 * How a row navigates. Deliberately the narrow `{ kind: "global" }` arm rather than the
 * whole `NavIntent` union: it keeps this module off `usePaletteRegistry` (and so off the
 * palette's whole import graph), while staying structurally assignable FROM the real
 * `navigate` — a function that accepts every NavIntent accepts this one.
 *
 * It must be the intent chokepoint and not `useUI.getState().openGlobalSettings` directly:
 * the frameless desktop launcher window mounts this same registry in a shell-less JS
 * context where uiStore mutations are inert, so a direct store call is a silent no-op
 * there. The chokepoint forwards the intent to the main window instead.
 */
export type SettingsNavigate = (intent: { kind: "global"; section?: string }) => void;

/**
 * Search vocabulary per section — the words an operator actually types when they want that
 * pane, which are usually NOT its nav label ("shortcuts" for Keyboard, "dark mode" for
 * Theme, "rag" for Knowledge, "a2a" for Delegates). The label and the group heading are
 * matched too (the DS matcher searches label · hint · group · keywords), so these are the
 * SYNONYMS only.
 *
 * `Record<SettingsSectionId, …>` makes it exhaustive: a section added to the leaf without
 * keywords fails to compile rather than shipping a row nobody can find by name.
 */
const SECTION_KEYWORDS: Record<SettingsSectionId, string[]> = {
  identity: ["identity", "name", "rename", "persona", "soul", "character", "avatar", "who"],
  access: ["operator", "owner", "org", "organization", "access", "permissions", "auth"],
  devices: ["devices", "pair", "pairing", "qr", "phone", "mobile", "tablet", "revoke"],
  model: ["model", "llm", "routing", "provider", "gateway", "connection", "oauth", "temperature", "caching"],
  behavior: ["behavior", "prompt", "personality", "style", "tone", "autonomy", "limits"],
  knowledge: ["knowledge", "rag", "memory", "embeddings", "index", "retrieval", "documents"],
  tracing: ["tracing", "trace", "langfuse", "spans", "observability", "debug"],
  secrets: ["secrets", "vault", "credentials", "keys", "manager"],
  publish: ["publish", "share", "link", "public", "hosted", "viewer"],
  plugins: ["plugins", "extensions", "install", "marketplace", "discover", "addons"],
  snapshot: ["snapshot", "export", "import", "backup", "restore", "clone", "portable"],
  tools: ["tools", "toolset", "allowlist", "functions", "python", "runtime"],
  mcp: ["mcp", "servers", "connectors", "stdio", "sse", "model context protocol"],
  skills: ["skills", "playbooks", "procedures", "recipes", "how-to"],
  subagents: ["subagents", "sub agents", "workers", "task", "delegation"],
  delegates: ["delegates", "a2a", "acp", "cli", "coding agent", "peers", "remote"],
  overview: ["overview", "status", "health", "runtime", "system", "diagnostics", "version"],
  fleet: ["fleet", "agents", "roster", "members", "spawn", "start", "stop"],
  telemetry: ["telemetry", "metrics", "usage", "cost", "tokens", "spend", "analytics"],
  theme: ["theme", "appearance", "colors", "dark mode", "light mode", "accent", "font"],
  chat: ["chat", "messages", "composer", "streaming", "reasoning", "transcript"],
  keybindings: ["keyboard", "shortcuts", "keybinding", "chord", "hotkey", "rebind", "keys"],
  // Never reached: `developer` is filtered out below (see SETTINGS_PALETTE_EXCLUDED), but
  // the map is exhaustive over the id union, so it still needs an entry.
  developer: ["developer", "flags", "debug", "experimental", "internal"],
};

/**
 * Sections that deliberately get NO palette row.
 *
 * Only `developer`, and the reason is the one quirk the two declarative gate axes cannot
 * express. The Developer panel is appended to "This console" at render time when
 * `developerPanelVisible(channel)` — a CHANNEL decision (a dev build, a non-prod channel,
 * or an explicit `?dev` reveal), not a `flag:` key and not the host-console axis. The seam
 * offers `flag` and `hostOnly` and nothing else, so an honest row is impossible:
 *
 *   • Ungated, it would list "Settings: Developer" to every production operator — exactly
 *     what the channel gate exists to prevent — and RUNNING it there would write
 *     "developer" into the persisted `settingsSection` before the surface's own gate drops
 *     it, leaving a dead id behind (see the header).
 *   • A `registerPaletteSource` could resolve the channel per read, but core ships ZERO
 *     dynamic sources on purpose: `usePaletteRegistry` wires the DS `CommandProvider` only
 *     when `hasPaletteSources()`, and the DS shows its "Searching…" spinner whenever any
 *     provider exists. One row is not worth putting a 120ms spinner in front of every
 *     keystroke in every console.
 *
 * So the panel keeps its existing entrances (the Settings rail, off prod) and the palette
 * stays honest about what it can gate. If the seam ever grows a channel axis, delete this.
 */
export const SETTINGS_PALETTE_EXCLUDED: readonly SettingsSectionId[] = ["developer"];

/**
 * One deep-link command per Settings section, in the section table's own order.
 *
 * Built from the table with EVERY GATE OPEN (`flagOn: () => true`, `onHost: true`,
 * `developerVisible: true`) — this is a row FACTORY, not a visibility decision. Asking for
 * the whole table and then dropping `SETTINGS_PALETTE_EXCLUDED` by name keeps the one
 * omission VISIBLE and testable; leaving `developerVisible` at its `false` default would
 * produce the identical list today by accident, and silently start shipping an ungated
 * Developer row the day that default changed. The gates come back out as `flag`/`hostOnly`
 * on each row for the host to apply per render.
 *
 * Rows land in the "Commands" group labelled "Settings: <Section>", matching the two
 * hand-written rows this replaces, so nothing about the root view's shape changes. The
 * trailing hint is the section's own nav heading (Agent · Capabilities · Box · This
 * console), which both disambiguates same-named rows and makes the heading searchable —
 * typing "capabilities" lists exactly the five capability panes.
 */
export function settingsPaletteCommands(navigate: SettingsNavigate): PaletteCommand[] {
  const excluded = new Set<string>(SETTINGS_PALETTE_EXCLUDED);
  return settingsSectionGroups({ flagOn: () => true, onHost: true, developerVisible: true }).flatMap(
    (group) =>
      group.sections
        .filter((section) => !excluded.has(section.id))
        .map((section): PaletteCommand => {
          // Widened to SectionMeta to read the OPTIONAL gate keys: `SettingsSection` is a
          // union of `as const` literals and a member that carries no `flag` has no such
          // property to read. Same view `sections.test.ts` asserts the quirks through.
          const meta: SectionMeta = section;
          return {
            id: `settings:${section.id}`,
            label: `Settings: ${section.label}`,
            group: "Commands",
            hint: group.label,
            // The lucide NAME resolved lazily (lib/lucideIcon → `icons[name] || Package`),
            // which is why the leaf stores names and `sections.test.ts` pins each one against
            // lucide's `icons` map: a deprecated alias would type-check, render correctly in
            // the Settings rail's static map, and silently be the Package box here.
            // Default size (18) — the size every other palette row's icon renders at
            // (coreSurfaces, the plugin-view rows); the Settings rail's own 15 would read as
            // a second, smaller class of row.
            icon: lucideIcon(section.icon),
            keywords: ["settings", ...SECTION_KEYWORDS[section.id]],
            // Gates copied through, never evaluated — the host resolves them per render.
            ...(meta.flag ? { flag: meta.flag } : {}),
            ...(meta.hostOnly ? { hostOnly: true } : {}),
            run: (ctx) => {
              navigate({ kind: "global", section: section.id });
              ctx.close();
            },
          };
        }),
  );
}
