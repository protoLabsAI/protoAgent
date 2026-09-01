import { describe, expect, it } from "vitest";

import { visibleSections } from "./sectionGate";
// The section table moved to ./sections (a leaf); SettingsSurface.tsx keeps the renderers,
// so the `category=` assertion below still reads it.
import sectionsSrc from "./sections.ts?raw";
import settingsSrc from "./SettingsSurface.tsx?raw";
import telemetrySrc from "../telemetry/TelemetrySurface.tsx?raw";

// #3017 — the Langfuse credentials must be reachable from a FLEET MEMBER's console.
//
// The issue is that tracing could only be configured through the environment, and nothing in
// the desktop app's member launch (`protoagent-server --port … --ui none`) puts LANGFUSE_* in
// that process's environment. A Settings home the member window never renders would leave that
// exactly as it was, one layer up: the four fields would be in /api/settings and in no DOM.
//
// That is precisely what filing them under Box ▸ Telemetry would have done — that section is
// `hostOnly` (boxSectionGate.test.ts pins it), so it is dropped off the host console. Hence the
// Agent-group "Tracing" section, which carries no gate at all.

const objOf = (id: string) => sectionsSrc.match(new RegExp(`\\{[^{}]*id: "${id}"[^{}]*\\}`))?.[0] ?? "";

describe("Settings ▸ Tracing is reachable from a fleet member's console (#3017)", () => {
  it("the section is declared, and in the Agent group", () => {
    expect(objOf("tracing")).not.toBe("");
    const agentGroup = sectionsSrc.split("const CAPABILITY_SECTIONS")[0];
    expect(agentGroup).toContain('id: "tracing"');
  });

  it("it renders the schema category the tracing fields carry", () => {
    // graph/settings_schema.py maps section "Tracing" → category "Observability"; a
    // SettingsCategoryPanel naming any other category renders an empty panel.
    expect(settingsSrc).toMatch(/tracing: \(\) => <SettingsCategoryPanel category="Observability"/);
  });

  it("it carries no hostOnly gate — the whole point (contrast Box ▸ Telemetry)", () => {
    expect(objOf("tracing")).not.toContain("hostOnly");
    expect(objOf("telemetry")).toContain("hostOnly: true");
  });

  it("so the real filter keeps it in a sister agent's window", () => {
    // The behaviour, not just the source: run the filter the surface actually calls.
    const list = [{ id: "knowledge" }, { id: "tracing" }, { id: "telemetry", hostOnly: true }];
    expect(visibleSections(list, () => true, false).map((s) => s.id)).toEqual(["knowledge", "tracing"]);
  });
});

describe("Box ▸ Telemetry's tracing chip (#3017)", () => {
  // The trace cell's title tells the operator where to go; on the host console the gear beside
  // the table is that place. It must name every field the setup needs — a chip that listed only
  // the toggle would send them to a dialog that cannot finish the job.
  const chip = telemetrySrc.match(/<QuickSetting\s+keys=\{\[[^\]]*tracing\.enabled[^\]]*\]\}[\s\S]*?\/>/)?.[0] ?? "";

  it("names all four tracing keys", () => {
    expect(chip).not.toBe("");
    for (const key of ["tracing.enabled", "tracing.host", "tracing.public_key", "tracing.secret_key"]) {
      expect(chip).toContain(`"${key}"`);
    }
  });

  it("stays SEPARATE from the telemetry chip, so telemetry keeps saving to the host layer", () => {
    // QuickSetting picks the host layer only when every key it edits is host-scoped.
    // telemetry.{enabled,retention_days} are host-scoped, tracing.* is agent-scoped: merged into
    // one chip the mixed set falls back to "agent" and the box-shared telemetry policy would
    // silently start writing the agent leaf.
    expect(chip).not.toContain("telemetry.enabled");
    expect(telemetrySrc).toContain('"telemetry.enabled"');
    expect(telemetrySrc).toContain('"telemetry.retention_days"');
  });

  it("the disabled-trace cell points at a section that exists", () => {
    const title = telemetrySrc.match(/title="Tracing is disabled[^"]*"/)?.[0] ?? "";
    expect(title).toContain("Settings ▸ Tracing");
    // The old copy named Box ▸ Telemetry's gear, which held two unrelated fields.
    expect(title).not.toContain("Settings ▸ Telemetry");
  });
});
