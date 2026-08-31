import { describe, expect, it } from "vitest";

import { visibleSections } from "./sectionGate";
import { settingsSections } from "./sections";
// The section table lives in ./sections (a leaf); SettingsSurface.tsx now holds only renderers.
import src from "./sections.ts?raw";
import surfaceSrc from "./SettingsSurface.tsx?raw";
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

  it("the REAL agent group is run through that gate — nav and id resolution both", () => {
    // The two cases above prove the gate on a synthetic table; this proves the real `secrets`
    // row is actually fed through it, via the same assembly the surface renders from.
    // Filtering only the nav would leave a persisted "secrets" id rendering the pre-release
    // panel on the prod channel — the exact failure #2120 is about.
    expect(settingsSections({ flagOn: () => false, onHost: true }).map((s) => s.id)).not.toContain(
      "secrets",
    );
    expect(
      settingsSections({ flagOn: (f) => f === "secrets-panel", onHost: true }).map((s) => s.id),
    ).toContain("secrets");
  });

  it("…and SettingsSurface renders from that assembly instead of re-deriving one", () => {
    // The one claim no test on the leaf can make: a surface that rebuilt its own section list
    // would keep every assertion in this file green while showing a flag-off panel. Source
    // guard because there is nothing else to hold — the component's own path is e2e-only.
    expect(surfaceSrc).toContain("settingsSectionGroups({");
  });

  it("the flag ships at tier dev", () => {
    expect(flags).toMatch(/id="secrets-panel"[\s\S]*?tier="dev"/);
  });
});
