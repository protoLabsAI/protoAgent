import { describe, expect, it } from "vitest";
import { AGENT_SECTIONS, settingsSections } from "./sections";

// Settings ▸ Devices graduated (ADR 0113 D8): the `settings.devices` flag came off once the
// "Allow devices on my network" → pair → revoke → back-to-loopback path passed in the desktop
// app. The golden in sections.test.ts turns every flag ON, so it can't tell a gated section
// from an ungated one — this pins that Devices shows with NO flag on, on the host console and
// in a member window alike (pairing an agent happens on the member being paired).
describe("Settings ▸ Devices is ungated", () => {
  it("carries no flag and no hostOnly gate", () => {
    const devices = AGENT_SECTIONS.find((s) => s.id === "devices");
    expect(devices).toBeDefined();
    expect(devices).not.toHaveProperty("flag");
    expect(devices).not.toHaveProperty("hostOnly");
  });

  it.each([true, false])("is in the nav with every flag off (onHost=%s)", (onHost) => {
    const ids = settingsSections({ flagOn: () => false, onHost }).map((s) => s.id);
    expect(ids).toContain("devices");
    // Where it sits: beside Operator & access, because it IS access.
    expect(ids.indexOf("devices")).toBe(ids.indexOf("access") + 1);
  });
});
