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
// ── What this module may import, and why the list is short ──────────────────────────
// CI has no bundle-size gate, so both edges are pinned by a SOURCE guard instead:
// `settingsPalette.test.ts` parses this file's own import list and asserts it exactly.
//
//   • NOT `lib/lucideIcon`. This is the one with a MEASURED cost. It is the obvious way to
//     turn the leaf's icon name into a glyph, and it is a lazy `import("lucide-react")` of
//     the whole icon set — which this build emits as its own chunk (737.8 kB, and nothing
//     else eagerly loads it). Using it here would make ⌘K the heaviest thing the desktop
//     Launcher window downloads, with every row flashing a Package box through its Suspense
//     fallback while the chunk arrives. That resolver is right where the name is chosen at
//     RUNTIME (a fleet archetype, a plugin's icon); these 23 are a closed set, so
//     `settings/sectionIcons` maps them statically — from glyphs already tree-shaken into
//     the entry chunk for the Settings rail, so the second consumer really is free.
//   • NOT `settings/SettingsSurface`. This one is about the module GRAPH, and it is worth
//     being precise, because the obvious justification is FALSE in today's build: main.tsx
//     imports App and Launcher statically and vite declares no `manualChunks`, so there is
//     exactly ONE entry chunk and the Launcher window already downloads (and evaluates) the
//     whole settings tree. Importing the surface here would cost zero extra bytes today —
//     measured, not assumed. What the guard buys is that `settings/sections.ts` (2 modules,
//     no React, no lucide, no panels) stays the only thing between a consumer that merely
//     NAMES a section and the ~105-module panel tree behind SettingsSurface. That is what
//     keeps this file's dependency list short enough to assert at all, and it is the
//     precondition for the split that WOULD pay bytes — lazy-loading App so the Launcher
//     stops shipping the console. `main.tsx` still importing both is pinned below, so the
//     day that changes, this comment is the thing that fails.
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
// applies its section id through `openGlobalSettings`, which writes it into the PERSISTED
// `settingsSection` (uiStore does not partialize that key) — the store is the intent's
// landing site, and it has to be authoritative or a repeat deep-link onto the section the
// dialog already holds is a no-op set that moves nothing. The section GATE runs later, in
// SettingsSurface, which falls back to the first visible section for an id it cannot
// resolve. So a row RUN while its section is gated off would leave a dead id in
// localStorage — a settings dialog that opens on Identity forever after, for no visible
// reason. Because `flag`/`hostOnly` ride the row, the window that RENDERS it has already
// answered both axes, so within a console window the run can't happen. The desktop Launcher
// is the one window that answers them for a DIFFERENT one: it loads `/app/`, so it gates
// with `isHostConsole() === true` and the host's `/api/flags`, then forwards the intent to
// whichever slug the main window sits on. Running `Settings: Telemetry` there, with the main
// window on a member slug, still lands the dead id. That hole predates these rows and is
// narrower with them (the `box:telemetry` row they replace carried no gate at all, so it was
// listed in every member window's OWN palette too); closing it means resolving the intent
// against the RECEIVING window's gates — in `applyNavIntent`, before it reaches the store —
// not by pre-filtering here, which is the fail-closed trap above.
import { sectionIcon } from "../settings/sectionIcons";
import { settingsSectionGroups } from "../settings/sections";
import type { PaletteCommand } from "../ext/paletteRegistry";
import type { SectionMeta, SettingsSectionId } from "../settings/sections";

/**
 * How a row navigates. Deliberately the narrow `{ kind: "global" }` arm rather than the
 * whole `NavIntent` union: it keeps this module off `palette/registry` (and so off the
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
 * Search vocabulary per section — the words an operator actually TYPES when they want that
 * pane, which are usually not its nav label ("shortcuts" for Keyboard, "dark mode" for Theme,
 * "api key" for Model, "rag" for Knowledge).
 *
 * Three rules, each learned by getting it wrong:
 *
 *  • SYNONYMS ONLY. The DS matcher searches label · hint · group · keywords as one lowercased
 *    haystack, substring per whitespace-separated term (`matchCommand`, mirrored in
 *    palette/rank's `matchCommand`). Every label here already begins "Settings: ", so a "settings"
 *    keyword — and the section's own label word — buys nothing.
 *  • TRUE of the PANE, not of the word. A keyword that sends an operator somewhere plausible
 *    but wrong is worse than no keyword: they stop searching. Behavior is Goal mode ·
 *    Watches · Compaction · Security (graph/settings_schema.py `_SECTION_CATEGORY`), so
 *    "prompt"/"persona" belong to Identity, whose panel is the SOUL editor; the DS ThemePanel
 *    has colors and shape and no typography, so "font" belongs to nothing.
 *  • Multi-word entries are FREE. Matching is per term over the joined haystack, so
 *    "api key" makes both `api key` and `key` land on Model, and costs one array slot.
 *
 * `Record<SettingsSectionId, …>` makes it exhaustive: a section added to the leaf without
 * vocabulary fails to compile rather than shipping a row nobody can find by name.
 */
const SECTION_KEYWORDS: Record<SettingsSectionId, string[]> = {
  identity: ["name", "rename", "persona", "soul", "soul.md", "system prompt", "instructions", "who"],
  // "a2a": the A2A bearer and the federation token (ADR 0066) are Identity-category fields
  // and render HERE, not on Delegates — which owns the word otherwise.
  access: ["owner", "org", "organization", "auth", "bearer", "token", "a2a", "federation", "project directory"],
  devices: ["pair", "pairing", "qr", "phone", "mobile", "tablet", "revoke"],
  model: ["llm", "provider", "api key", "keys", "gateway", "litellm", "routing", "connection", "oauth", "temperature", "sampling", "caching", "runtime"],
  behavior: ["goals", "goal mode", "autonomy", "watches", "compaction", "middleware", "background", "security", "redaction", "egress", "self-improvement"],
  knowledge: ["rag", "recall", "memory", "embeddings", "retrieval", "ingestion", "index", "documents", "history"],
  tracing: ["trace", "langfuse", "spans", "observability", "debug"],
  secrets: ["vault", "credentials", "keys", "manager", "external"],
  publish: ["share", "link", "public", "hosted", "viewer", "transcript"],
  plugins: ["extensions", "integrations", "install", "uninstall", "enable", "disable", "addons", "marketplace"],
  snapshot: ["export", "import", "backup", "restore", "clone", "migrate", "portable"],
  tools: ["toolset", "allowlist", "functions", "python", "runtime", "execute code", "filesystem", "work folders"],
  mcp: ["model context protocol", "servers", "connectors", "stdio", "sse"],
  skills: ["playbooks", "procedures", "recipes", "how-to", "skill.md"],
  subagents: ["workers", "task", "delegation", "roles"],
  // "delegation" landed on Subagents alone, which is the wrong half of a real ambiguity:
  // subagents are in-process `task()` workers, delegates are the external A2A/ACP agents.
  // Both are delegation; the word must reach both.
  delegates: ["a2a", "acp", "cli", "coding agent", "claude code", "codex", "peers", "remote", "delegation"],
  overview: ["status", "health", "runtime", "system", "diagnostics", "version", "storage", "disk", "about"],
  // The tail is the "Box runtime" QuickSetting in this panel's header (FleetManagerPanel's
  // BOX_RUNTIME_KEYS: network.bind · fleet.port_base · discovery ports/mDNS · keep-warm).
  // It is the ONLY home of those knobs in the whole dialog, and no word on any row reached
  // it — "port" and "network" are what an operator types, and neither is in a label.
  fleet: ["agents", "roster", "members", "spawn", "new agent", "start", "stop", "archetype", "box runtime", "network", "port", "discovery", "keep-warm"],
  telemetry: ["metrics", "usage", "cost", "spend", "tokens", "analytics", "rollup"],
  theme: ["appearance", "colors", "dark mode", "light mode", "accent", "brand", "palette", "contrast", "preset"],
  chat: ["transcript", "usage", "tokens", "cost", "context window", "footer", "meter"],
  keybindings: ["shortcuts", "keybinding", "chord", "hotkey", "rebind", "keys"],
  // Never reached: `developer` is filtered out below (see SETTINGS_PALETTE_EXCLUDED), but the
  // map is exhaustive over the id union, so it still needs an entry.
  developer: ["flags", "channel", "experimental", "internal", "debug"],
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
 *     dynamic sources on purpose: `palette/registry` wires the `CommandProvider` only
 *     when `hasPaletteSources()`, and the DS shows its "Searching…" spinner whenever any
 *     provider exists. One row is not worth putting a 120ms spinner in front of every
 *     keystroke in every console.
 *   • `palette/registry` could register/unregister the row from an EFFECT keyed on the
 *     channel — it works, and it costs no provider. It also makes core's first stateful
 *     palette registration, which is a pattern the next five special cases would copy, to
 *     reach a panel that is off-prod only and already one click away in the Settings rail.
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
 * hand-written rows this replaces, so the root view's structure is unchanged. The trailing
 * hint is the section's own nav heading (Agent · Capabilities · Box · This console), which
 * both disambiguates same-named rows and makes the heading searchable — typing
 * "capabilities" lists exactly the five capability panes. Each row wears the same glyph its
 * section wears in the Settings rail, so ⌘K reads like the rail it deep-links into. (The
 * four hand-written Commands rows took glyphs of their own at the same time — see
 * palette/registry: the row has no icon gutter, so a half-glyphed group steps.)
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
            // Default size (18) — what every other palette row's icon renders at (the
            // plugin-view rows, the chat row); the Settings rail's own 15 would read as a
            // second, smaller class of row.
            icon: sectionIcon(section.icon),
            keywords: SECTION_KEYWORDS[section.id],
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
