import { describe, expect, it } from "vitest";

import telemetrySrc from "../telemetry/TelemetrySurface.tsx?raw";

const chips = [...telemetrySrc.matchAll(/<QuickSetting[\s\S]*?\/>/g)].map((match) => match[0]);
const chipWith = (key: string) => chips.find((chip) => chip.includes(`"${key}"`)) ?? "";

describe("Box ▸ Telemetry settings reachability (#3032)", () => {
  it("offers every box-shared telemetry and prompt-retention field together", () => {
    const chip = chipWith("telemetry.enabled");
    expect(chip).not.toBe("");
    for (const key of [
      "telemetry.enabled",
      "telemetry.retention_days",
      "prompts.capture",
      "prompts.retention_days",
      "prompts.max_calls",
    ]) {
      expect(chip).toContain(`"${key}"`);
    }
    expect(chip).not.toContain('"telemetry.fleet_trace_export"');
  });

  it("keeps the agent-scoped fleet export setting in its own shortcut", () => {
    const chip = chipWith("telemetry.fleet_trace_export");
    expect(chip).not.toBe("");
    expect(chip).not.toContain('"telemetry.enabled"');
    expect(chip).not.toContain('"prompts.capture"');
  });
});
