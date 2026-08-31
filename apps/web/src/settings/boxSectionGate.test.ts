import { describe, expect, it } from "vitest";

import { settingsSectionGroups } from "./sections";
import { visibleSections } from "./sectionGate";
// The section table lives in ./sections (a leaf) — the object literals this file greps are
// there, not in SettingsSurface.tsx, which now only holds the render functions.
import src from "./sections.ts?raw";

// Settings ▸ Box on a sister agent's console. Overview + Telemetry read the FOCUSED agent's
// endpoints (/api/runtime, /api/telemetry), so they'd mean something different in a member
// window and stay `hostOnly`. Fleet does not: `/api/fleet` is a hub path (never slug-scoped,
// see lib/api.ts `isHubPath`), so it names the same fleet from any window — every sister agent
// manages the roster its hub does, which is the whole point of un-gating it.
//
// Behavior first — exercise the REAL filter on both axes; source guards second, for the edit
// that drops a `hostOnly:` key (type-checks, renders fine on the host, silently exposes an
// agent-scoped panel under a "Box" heading in every member window).

const BOX = [
  { id: "overview", hostOnly: true },
  { id: "fleet" },
  { id: "telemetry", hostOnly: true },
];

const on = () => true;

describe("Settings ▸ Box narrows (not disappears) off the host console", () => {
  it("host console → all three Box sections", () => {
    expect(visibleSections(BOX, on, true).map((s) => s.id)).toEqual(["overview", "fleet", "telemetry"]);
  });

  it("sister agent's window → Fleet survives, the agent-scoped pair is dropped", () => {
    expect(visibleSections(BOX, on, false).map((s) => s.id)).toEqual(["fleet"]);
  });

  it("the group is non-empty off the host, so the Fleet deep-link still resolves", () => {
    // The regression this guards: dropping the whole Box group made openGlobalSettings("fleet")
    // resolve to nothing and fall back to the first section — a dead menu item.
    expect(visibleSections(BOX, on, false)).not.toHaveLength(0);
  });

  it("the two gates compose — a flag-off section stays dropped on the host", () => {
    const list = [{ id: "fleet" }, { id: "secrets", flag: "secrets-panel" }];
    expect(visibleSections(list, () => false, true).map((s) => s.id)).toEqual(["fleet"]);
  });

  it("the real Box sections carry the intended hostOnly keys", () => {
    const objOf = (id: string) => src.match(new RegExp(`\\{[^{}]*id: "${id}"[^{}]*\\}`))?.[0] ?? "";
    expect(objOf("overview")).toContain("hostOnly: true");
    expect(objOf("telemetry")).toContain("hostOnly: true");
    expect(objOf("fleet")).not.toBe("");
    expect(objOf("fleet")).not.toContain("hostOnly");
  });

  it("the Box group renders whenever it has sections, not only on the host", () => {
    // Behaviour now that group assembly is a pure function: off the host the group must still
    // be there, narrowed to Fleet, rather than vanishing from the nav.
    const labels = (onHost: boolean) =>
      settingsSectionGroups({ flagOn: on, onHost }).map((g) => g.label);
    expect(labels(true)).toContain("Box");
    expect(labels(false)).toContain("Box");
    const box = settingsSectionGroups({ flagOn: on, onHost: false }).find((g) => g.label === "Box");
    expect(box?.sections.map((s) => s.id)).toEqual(["fleet"]);
  });

  it("…because the group is dropped on EMPTINESS, never on the host axis", () => {
    // The other half of the same rule, and the one behaviour the real table can't demonstrate:
    // Fleet carries neither gate, so BOX_SECTIONS is never actually empty. Source guard, so the
    // edit that swaps the condition back to `onHost ? …` (the original bug) is caught.
    expect(src).toMatch(/\.\.\.\(boxSections\.length \? \[\{ label: GROUP_LABELS\.box/);
    expect(src).not.toMatch(/onHost \? \[\{ label: GROUP_LABELS\.box/);
  });
});
