import { describe, expect, it } from "vitest";

import { visibleSections } from "./sectionGate";
import src from "./SettingsSurface.tsx?raw";
import flags from "../../../../runtime/flags.py?raw";

// Settings ▸ Secrets is gated to the dev channel (ADR 0068). The external secrets manager's
// connect/test/sync flow stays behind `secrets-panel` until it's exercised end to end, so it
// only shows on the dev channel / via override (#2120).
//
// Behavior first (the QA panel's fix-first on the original head): exercise the REAL filter
// with the flag both ways — a grep-only test would stay green if the gating itself broke.
// Source guards second, for the failure mode of someone deleting the `flag:` key during an
// unrelated edit: that type-checks, renders fine on their machine, and silently re-exposes a
// pre-release panel on the prod channel.

const SECTIONS = [
  { id: "identity" },
  { id: "secrets", flag: "secrets-panel" },
  { id: "plugins" },
];

describe("Settings ▸ Secrets is flag-gated (#2120)", () => {
  it("flag off → the secrets section is dropped; unflagged sections survive", () => {
    const out = visibleSections(SECTIONS, () => false);
    expect(out.find((s) => s.id === "secrets")).toBeUndefined();
    expect(out.map((s) => s.id)).toEqual(["identity", "plugins"]);
  });

  it("flag on → the secrets section is present, nothing else changes", () => {
    const out = visibleSections(SECTIONS, (id) => id === "secrets-panel");
    expect(out.find((s) => s.id === "secrets")).toBeDefined();
    expect(out).toHaveLength(SECTIONS.length);
  });

  it("the real secrets Section carries the flag (order-insensitive)", () => {
    // Extract the one object literal containing id: "secrets" and assert the flag key is
    // inside it — survives key reordering and reformatting, unlike a cross-key regex.
    const obj = src.match(/\{[^{}]*id: "secrets"[^{}]*\}/)?.[0] ?? "";
    expect(obj).not.toBe("");
    expect(obj).toContain('flag: "secrets-panel"');
  });

  it("SettingsSurface routes every section list through the pure gate", () => {
    // The component must call the SAME visibleSections this test exercises — and the
    // agent group (where secrets lives) must go through shown().
    expect(src).toContain('import { visibleSections } from "./sectionGate"');
    expect(src).toMatch(/const shown = \(list: Section\[\]\) => visibleSections\(list, flagOn\)/);
    expect(src).toMatch(/shown\(AGENT_SECTIONS\)/);
  });

  it("the flag ships at tier dev", () => {
    expect(flags).toMatch(/id="secrets-panel"[\s\S]*?tier="dev"/);
  });
});
