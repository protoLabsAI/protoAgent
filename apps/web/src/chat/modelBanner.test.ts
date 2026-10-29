import { describe, expect, it } from "vitest";
import { needsModelBanner } from "./modelBanner";

describe("needsModelBanner", () => {
  it("treats older servers and pending runtime data as configured", () => {
    expect(needsModelBanner({})).toBe(false);
    expect(needsModelBanner(undefined)).toBe(false);
  });
  it("shows only for an explicitly unconfigured model and clears on the next poll", () => {
    expect(needsModelBanner({ model_configured: false })).toBe(true);
    expect(needsModelBanner({ model_configured: true })).toBe(false);
  });
});
