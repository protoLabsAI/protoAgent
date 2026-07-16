import { describe, expect, it } from "vitest";

import { boxSectionIds } from "./boxSections";

describe("boxSectionIds", () => {
  it("host console shows the whole Box group, overview → fleet → telemetry", () => {
    expect(boxSectionIds(true)).toEqual(["overview", "fleet", "telemetry"]);
  });

  it("a slug window keeps Fleet and drops the box-shared sections (#1999)", () => {
    // The regression this guards: when `fleet` was host-only, the switcher's "+ New agent"
    // deep-link had no section to resolve to, and SettingsSurface's
    // `?? sections[0]` fallback silently rendered an unrelated Agent section instead.
    expect(boxSectionIds(false)).toEqual(["fleet"]);
  });

  it("fleet is reachable from every window", () => {
    for (const onHost of [true, false]) expect(boxSectionIds(onHost)).toContain("fleet");
  });
});
