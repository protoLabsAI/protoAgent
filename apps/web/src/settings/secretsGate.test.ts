import { describe, expect, it } from "vitest";
import src from "./SettingsSurface.tsx?raw";
import flags from "../../../../runtime/flags.py?raw";

// Settings ▸ Secrets is gated to the dev channel (ADR 0068). The external secrets manager's
// connect/test/sync flow stays behind `secrets-panel` until it's exercised end to end, so it
// only shows on the dev channel / via override (#2120).
//
// Source-level because the failure mode is someone deleting the `flag:` key during an
// unrelated edit: that type-checks, renders fine on their machine, and silently re-exposes a
// pre-release panel on the prod channel.
describe("Settings ▸ Secrets is flag-gated", () => {
  it("the section declares the flag", () => {
    expect(src).toMatch(/id: "secrets"[^}]*flag: "secrets-panel"/);
  });

  it("flag-off sections are filtered from nav AND from id resolution", () => {
    // Filtering only the nav would leave a persisted "secrets" id rendering the panel.
    expect(src).toContain("const shown = (list: Section[])");
    expect(src).toMatch(/const sections = \[\s*\.\.\.agentSections/);
  });

  it("the flag ships at tier dev", () => {
    expect(flags).toMatch(/id="secrets-panel"[\s\S]*?tier="dev"/);
  });
});
