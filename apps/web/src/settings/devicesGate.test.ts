import { describe, expect, it } from "vitest";
import src from "./SettingsSurface.tsx?raw";
import flags from "../../../../runtime/flags.py?raw";

// Settings ▸ Devices is gated OFF (ADR 0068). The pairing flow behind it stopped the desktop
// app from starting four separate times — each fix correct, each exposing the next layer — so
// it stays hidden until the whole path is exercised in the desktop app itself.
//
// Source-level because the failure mode is someone deleting the `flag:` key during an
// unrelated edit: that type-checks, renders fine on their machine, and silently re-exposes a
// flow with a track record of bricking the app.
describe("Settings ▸ Devices is flag-gated", () => {
  it("the section declares the flag", () => {
    expect(src).toMatch(/id: "devices"[^}]*flag: "settings\.devices"/);
  });

  it("flag-off sections are filtered from nav AND from id resolution", () => {
    // Filtering only the nav would leave a persisted "devices" id rendering the panel.
    expect(src).toContain("const shown = (list: Section[])");
    expect(src).toMatch(/const sections = \[\s*\.\.\.agentSections/);
  });

  it("the flag ships OFF", () => {
    expect(flags).toMatch(/id="settings\.devices"[\s\S]*?tier="off"/);
  });
});
