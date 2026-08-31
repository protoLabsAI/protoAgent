import { describe, expect, it } from "vitest";

import {
  ALL_SECTIONS,
  CONSOLE_SECTIONS,
  DEVELOPER_SECTION,
  settingsSectionGroups,
  settingsSections,
} from "./sections";
import type { SectionMeta } from "./sections";
import gateSrc from "./sectionGate.ts?raw";
import sectionsSrc from "./sections.ts?raw";

// sections.ts exists so a consumer that only needs to NAME a settings section — ⌘K, a deep
// link, the desktop Launcher window (which mounts the palette registry but never mounts App) —
// can read the table without importing the settings tree. `import … from "./SettingsSurface"`
// eagerly welds ~90 modules / ~800 KB of panel source (FleetManagerPanel, PluginsSurface,
// ToolsPanel, TelemetrySurface, 20-odd panels and 26 lucide components) onto that path, and CI
// has NO bundle-size gate, so the regression would ship silently. Hence a SOURCE guard: the
// only thing that can be checked is what the module is allowed to import.

/** Every module specifier the file imports (value or type), in source order. */
function importSpecifiers(src: string): string[] {
  return [...src.matchAll(/(?:^|\n)\s*(?:import|export)\b[^\n;]*?from\s*["']([^"']+)["']/g)].map((m) => m[1]);
}

/** Specifiers imported for their VALUES — `import type …` / `export type …` excluded. */
function valueImportSpecifiers(src: string): string[] {
  return [...src.matchAll(/(?:^|\n)\s*(?:import|export)\s+(?!type\b)[^\n;]*?from\s*["']([^"']+)["']/g)].map(
    (m) => m[1],
  );
}

describe("settings/sections.ts is a leaf", () => {
  it("imports nothing but ./sectionGate", () => {
    expect(importSpecifiers(sectionsSrc)).toEqual(["./sectionGate", "./sectionGate"]);
    expect(valueImportSpecifiers(sectionsSrc)).toEqual(["./sectionGate"]);
  });

  it("…and ./sectionGate is itself import-free, so the leaf's cost is its own text", () => {
    expect(importSpecifiers(gateSrc)).toEqual([]);
  });

  it("pulls in no React, no lucide components and no panels", () => {
    for (const banned of ["react", "lucide-react", "@protolabsai/ui", "@tanstack/react-query"]) {
      expect(importSpecifiers(sectionsSrc)).not.toContain(banned);
    }
    // `icon:` must be the lucide NAME. A component reference (bare `icon: Sparkles`) is what
    // would drag lucide-react back in; every entry must quote its glyph.
    expect(sectionsSrc).not.toMatch(/icon: [A-Z]/);
    for (const s of ALL_SECTIONS) expect(typeof s.icon).toBe("string");
  });
});

describe("the section table's two render-time quirks survive extraction", () => {
  const on = () => true;

  it('"This console" is NOT run through the gate — Developer is APPENDED by channel', () => {
    // The quirk a naive consumer gets wrong: console sections carry no flag and no host axis,
    // and Developer's visibility is a CHANNEL decision made at render time, not a `flag:` key.
    // Read through the widened view: the `as const` literals have no `flag`/`hostOnly` KEY at
    // all, which is the same claim one level up — this asserts it at runtime too.
    const consoleMeta: readonly SectionMeta[] = CONSOLE_SECTIONS;
    const developerMeta: SectionMeta = DEVELOPER_SECTION;
    expect(consoleMeta.some((s) => s.flag || s.hostOnly)).toBe(false);
    expect(developerMeta.flag).toBeUndefined();

    const consoleOf = (developerVisible: boolean) =>
      settingsSectionGroups({ flagOn: () => false, onHost: false, developerVisible })
        .find((g) => g.label === "This console")
        ?.sections.map((s) => s.id);
    // Every gate off, off the host — the console group is untouched either way.
    expect(consoleOf(false)).toEqual(["theme", "chat", "keybindings"]);
    expect(consoleOf(true)).toEqual(["theme", "chat", "keybindings", "developer"]);
  });

  it("the resolvable set is exactly the flattened nav", () => {
    const groups = settingsSectionGroups({ flagOn: on, onHost: true, developerVisible: true });
    expect(settingsSections({ flagOn: on, onHost: true, developerVisible: true })).toEqual(
      groups.flatMap((g) => g.sections),
    );
  });
});

describe("section ids and order are a contract", () => {
  it("the persisted uiStore ids and their order are unchanged", () => {
    // `settingsSection` is persisted by id: renaming or reordering one silently relocates
    // (or blanks) every operator's last-open section. Golden, deliberately spelled out.
    expect(settingsSections({ flagOn: () => true, onHost: true, developerVisible: true }).map((s) => s.id)).toEqual([
      "identity",
      "access",
      "devices",
      "model",
      "behavior",
      "knowledge",
      "tracing",
      "secrets",
      "publish",
      "plugins",
      "snapshot",
      "tools",
      "mcp",
      "skills",
      "subagents",
      "delegates",
      "overview",
      "fleet",
      "telemetry",
      "theme",
      "chat",
      "keybindings",
      "developer",
    ]);
  });

  it("the group labels and their order are unchanged", () => {
    expect(settingsSectionGroups({ flagOn: () => true, onHost: true }).map((g) => g.label)).toEqual([
      "Agent",
      "Capabilities",
      "Box",
      "This console",
    ]);
  });

  it("every id is unique", () => {
    const ids = ALL_SECTIONS.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("every declared section is reachable — ALL_SECTIONS is exactly the ungated nav", () => {
    // ALL_SECTIONS (the domain of SettingsSectionId/Icon, hence of SettingsSurface's two
    // exhaustive maps) and settingsSectionGroups are two hand-kept compositions of the same
    // five arrays. Drift one way is a TYPE error — a row that reaches a group but not
    // ALL_SECTIONS isn't a SettingsSection. Drift the other way is not: adding to ALL_SECTIONS
    // alone mints an id that demands a renderer and an icon nothing can ever reach. This is
    // the assertion that catches that direction.
    const reachable = settingsSections({ flagOn: () => true, onHost: true, developerVisible: true });
    expect(reachable.map((s) => s.id)).toEqual(ALL_SECTIONS.map((s) => s.id));
  });
});
