import { describe, expect, it } from "vitest";
import ts from "typescript";
import { icons } from "lucide-react";

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
// pulls ~105 first-party modules of panel source (FleetManagerPanel, PluginsSurface,
// ToolsPanel, TelemetrySurface, 20-odd panels and 26 lucide components) onto that path.
//
// What that is worth is a graph property, NOT a byte one — today both desktop windows load a
// single entry chunk that already contains the panel tree (pinned in
// app/settingsPalette.test.ts). It is worth a guard anyway, for two reasons: a short,
// assertable import list is the only enforceable form the invariant HAS, since CI has no
// bundle-size gate; and the leaf is the precondition for ever splitting the settings tree out
// of that chunk. So: a SOURCE guard on what the module is allowed to import.
//
// That guard PARSES rather than greps, and the distinction is the whole ballgame. It began as a
// pair of regexes that required the `from` clause to sit on the same line as its `import`
// keyword — so the prettier-wrapped shape (`import {\n  Sparkles,\n} from "lucide-react";`,
// i.e. what any formatter emits past printWidth, and exactly the shape SettingsSurface's own
// 25-name lucide import would take) matched NOTHING, as did a bare `import "./x.css"` and a
// `() => import("./SettingsSurface")`. Every assertion below stayed green with the heavy tree
// back in the leaf. A guard that is the sole protection for an invariant cannot be
// shape-sensitive, so this one asks the TypeScript parser instead of a regex.

type ImportRef = { spec: string; typeOnly: boolean };

/**
 * Every module specifier `src` imports, in source order and in EVERY shape TypeScript
 * understands: single-line and wrapped static imports, `import type`, bare side-effect
 * `import "x"` (no clause at all, so no `from`), `export … from`, `import x = require("x")`,
 * and dynamic `import("x")` — a lazy chunk is still a chunk this module owns. A dynamic import
 * whose specifier isn't a literal is reported as "<computed>" so it fails the equality
 * assertions rather than vanishing.
 */
function scanImports(src: string, fileName = "sections.ts"): ImportRef[] {
  const sf = ts.createSourceFile(fileName, src, ts.ScriptTarget.Latest, false, ts.ScriptKind.TS);
  const found: ImportRef[] = [];
  const text = (n: ts.Node | undefined): string | undefined =>
    n && ts.isStringLiteralLike(n) ? n.text : undefined;
  const visit = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node)) {
      // No import clause at all is a side-effect import: it EXECUTES the module, so it is the
      // most expensive shape there is, never a type-only one.
      const spec = text(node.moduleSpecifier);
      if (spec !== undefined) found.push({ spec, typeOnly: node.importClause?.isTypeOnly ?? false });
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier) {
      const spec = text(node.moduleSpecifier);
      if (spec !== undefined) found.push({ spec, typeOnly: node.isTypeOnly });
    } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
      const spec = text(node.moduleReference.expression);
      if (spec !== undefined) found.push({ spec, typeOnly: node.isTypeOnly });
    } else if (ts.isCallExpression(node)) {
      const callee = node.expression;
      const dynamic =
        callee.kind === ts.SyntaxKind.ImportKeyword ||
        (ts.isIdentifier(callee) && callee.text === "require");
      if (dynamic) found.push({ spec: text(node.arguments[0]) ?? "<computed>", typeOnly: false });
    }
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(sf, visit);
  return found;
}

const specifiersOf = (src: string): string[] => scanImports(src).map((i) => i.spec);
const valueSpecifiersOf = (src: string): string[] =>
  scanImports(src)
    .filter((i) => !i.typeOnly)
    .map((i) => i.spec);

describe("the leaf guard itself", () => {
  // The guard is the only thing keeping sections.ts import-light, so it gets its own test: a
  // net with a hole in it reads exactly like a net. Each mutation below is a real regression
  // this file must catch, and each was INVISIBLE to the line-anchored regexes this replaced
  // (they returned just the two "./sectionGate" entries for the whole block).
  it("sees an import in every shape a future edit could take", () => {
    const mutated = [
      'import {\n  Sparkles,\n  KeyRound,\n} from "lucide-react";',
      'import "./sections.css";',
      'import {\n  useFlagPredicate,\n} from "../flags/flags";',
      'import type {\n  LucideIcon,\n} from "lucide-react";',
      'export * from "./SettingsSurface";',
      sectionsSrc,
      'const lazyPanel = () => import("./TelemetrySurface");',
    ].join("\n");
    expect(specifiersOf(mutated)).toEqual([
      "lucide-react",
      "./sections.css",
      "../flags/flags",
      "lucide-react",
      "./SettingsSurface",
      "./sectionGate",
      "./sectionGate",
      "./TelemetrySurface",
    ]);
    // …and the type/value split survives wrapping too: only the `import type` is erased.
    expect(valueSpecifiersOf(mutated)).toEqual([
      "lucide-react",
      "./sections.css",
      "../flags/flags",
      "./SettingsSurface",
      "./sectionGate",
      "./TelemetrySurface",
    ]);
  });
});

describe("settings/sections.ts is a leaf", () => {
  it("imports nothing but ./sectionGate", () => {
    expect(specifiersOf(sectionsSrc)).toEqual(["./sectionGate", "./sectionGate"]);
    expect(valueSpecifiersOf(sectionsSrc)).toEqual(["./sectionGate"]);
  });

  it("…and ./sectionGate is itself import-free, so the leaf's cost is its own text", () => {
    // sectionGate is the leaf's only edge, so this pair of assertions IS the transitive
    // closure: {sections.ts, sectionGate.ts} and nothing else.
    expect(scanImports(gateSrc, "sectionGate.ts")).toEqual([]);
  });

  it("pulls in no React, no lucide components and no panels", () => {
    for (const banned of ["react", "lucide-react", "@protolabsai/ui", "@tanstack/react-query"]) {
      expect(specifiersOf(sectionsSrc)).not.toContain(banned);
    }
    // `icon:` must be the lucide NAME. A component reference (bare `icon: Sparkles`) is what
    // would drag lucide-react back in; every entry must quote its glyph.
    expect(sectionsSrc).not.toMatch(/icon: [A-Z]/);
    for (const s of ALL_SECTIONS) expect(typeof s.icon).toBe("string");
  });

  it("every icon name is CANONICAL — a key of lucide's own `icons` map, not a stale alias", () => {
    // `icons` is lucide's registry of live names; its top-level named exports are a superset,
    // because it keeps DEPRECATED aliases exported after dropping them from the map. So an
    // alias satisfies a static `Record<SettingsSectionIcon, LucideIcon>` (settings/sectionIcons)
    // and renders perfectly, while being invisible to anything that resolves the string —
    // `icons[name] || Package`, which is what lib/lucideIcon does for a name chosen at runtime.
    // `icon: "BarChart3"` shipped exactly that way: type-checked, correct in the rail, silently
    // the Package box for a by-name consumer — the same glyph as Agent ▸ Snapshot, so it read
    // as intentional. Both of the table's consumers are static today and neither would notice;
    // the point of storing NAMES is that the next one (a plugin, the Launcher) need not be.
    for (const s of ALL_SECTIONS) {
      expect(
        icons[s.icon as keyof typeof icons],
        `${s.id}: icon "${s.icon}" is not a key of lucide's \`icons\` map (a deprecated alias?)`,
      ).toBeDefined();
    }
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
