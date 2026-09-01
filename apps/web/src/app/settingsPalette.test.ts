// The generated Settings deep-links (audit finding 06). Four claims are load-bearing, and
// each is a failure this console can produce SILENTLY — which is why each gets a test rather
// than a code comment:
//
//   1. COVERAGE follows the section table. The bug being fixed is a hand-written list of
//      three rows that nobody extended when twenty more sections landed. If these tests only
//      checked "some rows exist" they would have passed before the fix too, so they pin the
//      row set against `settings/sections.ts` itself.
//   2. GATES ride the row; they are not applied when the row is built. `useFlagPredicate`
//      fails CLOSED while /api/flags is in flight, so a `flag` resolved at registration hides
//      its row forever — on every channel, in a way no flags stub reproduces.
//   3. The WIRING hands both of those to the seam. A perfect row factory nothing registers,
//      or one registered through a filter, looks identical from the factory's own tests — so
//      the last block reads the registry back after the module-load registration.
//   4. The module stays a LEAF. Importing `settings/SettingsSurface` for the same ids would
//      weld the whole panel tree onto ⌘K and onto the desktop Launcher window, and CI has no
//      bundle-size gate to notice. Only a source guard can catch that.
import ts from "typescript";
import { describe, expect, it, vi } from "vitest";

import {
  SETTINGS_PALETTE_EXCLUDED,
  settingsPaletteCommands,
  type SettingsNavigate,
} from "./settingsPalette";
import { ALL_SECTIONS, settingsSections } from "../settings/sections";
import type { SectionMeta } from "../settings/sections";
import { visiblePaletteCommands } from "../ext/paletteRegistry";
import paletteSrc from "./settingsPalette.ts?raw";
// Imported for its MODULE-LOAD side effect: `usePaletteRegistry` is where core registers
// these rows through the public seam. The block at the bottom reads the registry back.
import "./usePaletteRegistry";

const nav: SettingsNavigate = () => {};
const rows = () => settingsPaletteCommands(nav);
const byId = (id: string) => rows().find((c) => c.id === id);
/** The section table read through the widened view, so the optional gate keys are readable. */
const meta = (id: string): SectionMeta => ALL_SECTIONS.find((s) => s.id === id)! as SectionMeta;

describe("coverage is derived from the section table, not hand-listed", () => {
  it("produces exactly one row per declared section, minus the documented exclusions", () => {
    // The whole point of the change: not "3 of 23" and not "some". The expected set is
    // COMPUTED from the table, so adding a section to sections.ts and forgetting this module
    // fails here rather than shipping an unreachable pane.
    const expected = ALL_SECTIONS.map((s) => s.id).filter(
      (id) => !SETTINGS_PALETTE_EXCLUDED.includes(id),
    );
    expect(rows().map((c) => c.id)).toEqual(expected.map((id) => `settings:${id}`));
    // …and that really is nearly everything: 22 of the 23 declared sections.
    expect(rows()).toHaveLength(ALL_SECTIONS.length - SETTINGS_PALETTE_EXCLUDED.length);
    expect(SETTINGS_PALETTE_EXCLUDED).toEqual(["developer"]);
  });

  it("covers the sections the audit named as palette-invisible", () => {
    // Before this module ⌘K deep-linked three sections: Settings, Settings: Fleet, Settings:
    // Telemetry. Spelled out rather than derived, because a derivation from the same table
    // the implementation reads could not fail — this is the human-legible claim.
    for (const id of [
      "theme",
      "keybindings",
      "model",
      "tools",
      "mcp",
      "skills",
      "subagents",
      "delegates",
      "secrets",
      "snapshot",
    ]) {
      expect(byId(`settings:${id}`), `no palette row for Settings ▸ ${id}`).toBeDefined();
    }
  });

  it("keeps the rows in the section table's own order, so the palette reads like the rail", () => {
    // settingsSections() flattens the groups in nav order; the rows must not re-sort.
    const navOrder = settingsSections({ flagOn: () => true, onHost: true, developerVisible: true })
      .map((s) => s.id)
      .filter((id) => !SETTINGS_PALETTE_EXCLUDED.includes(id));
    expect(rows().map((c) => c.id)).toEqual(navOrder.map((id) => `settings:${id}`));
  });

  it("every id is unique and namespaced, so a row can't silently evict another command", () => {
    // registerPaletteCommand is last-wins by id: a collision with an existing core row
    // ("settings", "plug:market", "fleet-room", "open") would REPLACE it, not warn.
    const ids = rows().map((c) => c.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const id of ids) expect(id.startsWith("settings:")).toBe(true);
    // The bare "Settings" command survives: `settings` is not `settings:<id>`.
    expect(ids).not.toContain("settings");
  });
});

describe("the row shape is the one the docs and the ranking claim", () => {
  it('labels read "Settings: <Section>" in the Commands group, hinted with the nav heading', () => {
    expect(byId("settings:keybindings")).toMatchObject({
      label: "Settings: Keyboard",
      group: "Commands",
      hint: "This console",
    });
    expect(byId("settings:fleet")).toMatchObject({ label: "Settings: Fleet", hint: "Box" });
    expect(byId("settings:tools")).toMatchObject({ label: "Settings: Tools", hint: "Capabilities" });
    expect(byId("settings:model")).toMatchObject({ label: "Settings: Model", hint: "Agent" });
    // Uniform, not just for the four spot-checked rows.
    for (const c of rows()) {
      expect(c.group).toBe("Commands");
      expect(c.label).toBe(`Settings: ${meta(c.id.slice("settings:".length)).label}`);
      expect(["Agent", "Capabilities", "Box", "This console"]).toContain(c.hint);
    }
  });

  it("keywords carry the section's own vocabulary, not just its label", () => {
    // A row findable only by its nav label is barely findable: nobody types "keybindings"
    // when they want to change a shortcut.
    const kw = (id: string) => byId(`settings:${id}`)?.keywords ?? [];
    expect(kw("keybindings")).toEqual(expect.arrayContaining(["shortcuts", "chord", "rebind"]));
    expect(kw("theme")).toEqual(expect.arrayContaining(["dark mode", "appearance"]));
    expect(kw("knowledge")).toEqual(expect.arrayContaining(["rag", "embeddings"]));
    expect(kw("delegates")).toEqual(expect.arrayContaining(["a2a", "acp"]));
    expect(kw("mcp")).toEqual(expect.arrayContaining(["servers", "connectors"]));
    for (const c of rows()) {
      expect(c.keywords?.[0]).toBe("settings");
      // Vocabulary BEYOND the shared "settings" prefix — an empty tail is the regression.
      expect((c.keywords ?? []).length).toBeGreaterThan(3);
    }
  });

  it("every row carries an icon, resolved from the leaf's lucide NAME", () => {
    // The leaf stores names, not components, so it stays import-light; this consumer feeds
    // them to lib/lucideIcon. `icon` being present is all that can be asserted here — that
    // the name is a REAL lucide glyph rather than a deprecated alias silently falling back to
    // the Package box is pinned in settings/sections.test.ts against lucide's `icons` map.
    for (const c of rows()) expect(c.icon, `${c.id} has no icon`).toBeTruthy();
  });
});

describe("gating is declarative — the rows are never pre-filtered", () => {
  it("flag-gated sections are LISTED, carrying the leaf's flag id as data", () => {
    // The failure mode this prevents: filtering on the flag at registration. Registration
    // runs at module load, before /api/flags has answered, and the predicate fails closed —
    // so the row would be computed as "hidden" once and never recomputed.
    expect(byId("settings:secrets")).toMatchObject({ flag: "secrets-panel" });
    expect(byId("settings:devices")).toMatchObject({ flag: "settings.devices" });
    expect(byId("settings:publish")).toMatchObject({ flag: "chat.publish" });
    // Nothing invented and nothing dropped: the row's flag is the section's flag, verbatim.
    for (const c of rows()) expect(c.flag).toBe(meta(c.id.slice("settings:".length)).flag);
  });

  it("host-only sections are LISTED, carrying hostOnly — and Fleet deliberately is not", () => {
    expect(byId("settings:overview")).toMatchObject({ hostOnly: true });
    expect(byId("settings:telemetry")).toMatchObject({ hostOnly: true });
    // /api/fleet is a hub path, so Fleet names the same fleet from a member window too.
    // Marking it hostOnly here would take the roster away from every sister agent's ⌘K.
    expect(byId("settings:fleet")?.hostOnly).toBeUndefined();
    for (const c of rows()) {
      expect(c.hostOnly).toBe(meta(c.id.slice("settings:".length)).hostOnly);
    }
  });

  it("an ungated section carries neither key, so the host's filter is a no-op for it", () => {
    // `{ flag: undefined }` would be harmless to `visiblePaletteCommands` but noisy to a
    // "why is this row hidden?" reader; the keys are omitted, not set to undefined.
    expect("flag" in byId("settings:theme")!).toBe(false);
    expect("hostOnly" in byId("settings:theme")!).toBe(false);
  });

  it("no row is disabled — a gated section vanishes rather than teasing an unrunnable pane", () => {
    // `disabled` keeps a row LISTED but unrunnable, which is right for Fleet Room (it can
    // explain itself) and wrong here: running a deep-link for a gated-off section writes its
    // id into the PERSISTED settingsSection before the surface's own gate drops it, leaving a
    // dead id in localStorage. Gating (not disabling) is what makes that unreachable.
    for (const c of rows()) expect(c.disabled).toBeUndefined();
  });
});

describe("running a row goes through the NavIntent chokepoint", () => {
  it("emits a serializable { kind: 'global', section } and closes the palette", () => {
    // NOT `useUI.getState().openGlobalSettings(...)`: the frameless desktop launcher mounts
    // this same registry in a shell-less JS context where store mutations are inert, so a
    // direct store call is a silent no-op there. The intent is a plain object precisely so it
    // can cross the window boundary as an event payload.
    const navigate = vi.fn();
    const close = vi.fn();
    const row = settingsPaletteCommands(navigate).find((c) => c.id === "settings:mcp")!;
    row.run({ close });
    expect(navigate).toHaveBeenCalledWith({ kind: "global", section: "mcp" });
    expect(close).toHaveBeenCalledTimes(1);
    expect(JSON.parse(JSON.stringify(navigate.mock.calls[0][0]))).toEqual({
      kind: "global",
      section: "mcp",
    });
  });

  it("every row navigates to its OWN section id — the id the uiStore persists", () => {
    // An off-by-one in the generator would deep-link the neighbouring pane, which reads as
    // "the palette is fine" until you notice you keep landing on Behavior.
    for (const c of rows()) {
      const navigate = vi.fn();
      settingsPaletteCommands(navigate)
        .find((r) => r.id === c.id)!
        .run({ close: () => {} });
      expect(navigate).toHaveBeenCalledWith({
        kind: "global",
        section: c.id.slice("settings:".length),
      });
    }
  });
});

describe("wired into the registry, the HOST applies the gates", () => {
  // The factory tests above prove the rows CARRY their gates; this one proves the wiring
  // actually hands them to the seam, and that the seam's read is what decides visibility.
  // Together they cover the regression neither half sees alone: rows that are correct but
  // never registered, or registered but gated at the wrong moment.
  const ids = (flagsOn: boolean, onHost: boolean) =>
    visiblePaletteCommands(() => flagsOn, onHost, "static").map((c) => c.id);
  const settingsIds = (flagsOn: boolean, onHost: boolean) =>
    ids(flagsOn, onHost).filter((id) => id.startsWith("settings:"));

  it("registers every row STATICALLY — core must not ship a dynamic source", () => {
    // `usePaletteRegistry` wires the DS CommandProvider the moment ANY source exists, and the
    // DS shows its "Searching…" spinner whenever a provider does — so one source here would
    // put a 120ms spinner in front of every keystroke in every console.
    expect(settingsIds(true, true)).toHaveLength(
      ALL_SECTIONS.length - SETTINGS_PALETTE_EXCLUDED.length,
    );
    expect(visiblePaletteCommands(() => true, true, "dynamic")).toEqual([]);
  });

  it("a flag-gated row is hidden while /api/flags is in flight and REVEALED when it lands", () => {
    // THE regression. Registration ran at module load — before this file could stub anything —
    // and the fail-closed predicate is applied here, at read time. A row filtered at
    // registration would be absent from BOTH reads, identically and permanently.
    expect(settingsIds(false, true)).not.toContain("settings:secrets");
    expect(settingsIds(true, true)).toContain("settings:secrets");
    expect(settingsIds(false, true)).toContain("settings:theme"); // ungated, listed either way
  });

  it("host-only rows drop in a member window; Fleet stays", () => {
    expect(settingsIds(true, false)).not.toContain("settings:telemetry");
    expect(settingsIds(true, false)).not.toContain("settings:overview");
    expect(settingsIds(true, false)).toContain("settings:fleet");
  });

  it("supersedes the hand-written rows instead of doubling up with them", () => {
    // The old ids are gone; nothing renders "Settings: Fleet" twice. The bare `Settings`
    // command (open wherever you left it) is deliberately kept.
    expect(ids(true, true)).not.toContain("box:fleet");
    expect(ids(true, true)).not.toContain("box:telemetry");
    expect(ids(true, true)).toContain("settings");
    const labels = visiblePaletteCommands(() => true, true, "static").map((c) => c.label);
    expect(labels.filter((l) => l === "Settings: Fleet")).toHaveLength(1);
  });

  it("Developer is registered nowhere — not statically, not dynamically", () => {
    expect(ids(true, true)).not.toContain("settings:developer");
    expect(visiblePaletteCommands(() => true, true).map((c) => c.id)).not.toContain(
      "settings:developer",
    );
  });
});

// ── Source guard: this module must not drag the settings tree onto the palette ───────
//
// The house pattern (settings/sections.test.ts), reproduced deliberately rather than
// imported: that file guards a DIFFERENT module, and a guard shared between two invariants
// is one edit away from being relaxed for the wrong one. It PARSES rather than greps,
// because the regex version of this guard was blind to exactly the shapes that matter — a
// prettier-wrapped multi-line import, a bare side-effect `import "./x.css"`, and a lazy
// `() => import("./SettingsSurface")` (still a chunk this module owns).
function specifiersOf(src: string): string[] {
  const sf = ts.createSourceFile("settingsPalette.ts", src, ts.ScriptTarget.Latest, false, ts.ScriptKind.TS);
  const found: string[] = [];
  const text = (n: ts.Node | undefined) => (n && ts.isStringLiteralLike(n) ? n.text : undefined);
  const visit = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node) || (ts.isExportDeclaration(node) && node.moduleSpecifier)) {
      const spec = text((node as ts.ImportDeclaration | ts.ExportDeclaration).moduleSpecifier);
      if (spec !== undefined) found.push(spec);
    } else if (ts.isCallExpression(node)) {
      const callee = node.expression;
      if (callee.kind === ts.SyntaxKind.ImportKeyword) found.push(text(node.arguments[0]) ?? "<computed>");
    }
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(sf, visit);
  return found;
}

describe("settingsPalette.ts stays off the settings panel tree", () => {
  it("the guard sees imports in every shape an edit could take", () => {
    // A net with a hole in it reads exactly like a net, so the guard is itself tested. Each
    // shape below was invisible to the line-anchored regexes this pattern replaced.
    expect(
      specifiersOf(
        [
          'import {\n  SettingsSurface,\n} from "../settings/SettingsSurface";',
          'import "./palette.css";',
          'export * from "../settings/SettingsSurface";',
          'const lazy = () => import("../settings/SettingsSurface");',
        ].join("\n"),
      ),
    ).toEqual([
      "../settings/SettingsSurface",
      "./palette.css",
      "../settings/SettingsSurface",
      "../settings/SettingsSurface",
    ]);
  });

  it("imports the LEAF and nothing from the settings panel tree", () => {
    // Exact list, not a denylist: a denylist only forbids what someone thought of, and the
    // cheap way back into the panel tree is a *new* import nobody anticipated (SettingsOverlay,
    // SettingsCategory, a panel's helper). Pinning the whole list makes any new edge a
    // conscious decision with this comment attached.
    expect(specifiersOf(paletteSrc)).toEqual([
      "../lib/lucideIcon",
      "../settings/sections",
      "../ext/paletteRegistry",
      "../settings/sections",
    ]);
  });

  it("…which means no SettingsSurface, no panels, and no heavy runtime deps", () => {
    const specs = specifiersOf(paletteSrc);
    for (const banned of [
      "../settings/SettingsSurface",
      "./SettingsSurface",
      "../settings/SettingsOverlay",
      "../settings/SettingsCategory",
      "../settings/FleetSurface",
      "../telemetry/TelemetrySurface",
      "../plugins/PluginsSurface",
      "@tanstack/react-query",
      "@protolabsai/ui/command-palette",
      "../state/uiStore",
      "./usePaletteRegistry",
    ]) {
      expect(specs, `settingsPalette.ts must not import ${banned}`).not.toContain(banned);
    }
    // Not even by substring — a re-export barrel or a deeper path would still pull it in.
    for (const s of specs) expect(s).not.toMatch(/SettingsSurface/);
  });
});
