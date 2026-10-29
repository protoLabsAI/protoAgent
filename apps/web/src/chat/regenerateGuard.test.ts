import { describe, expect, it } from "vitest";

import { regenerateBlocked } from "./regenerateGuard";

describe("regenerateBlocked", () => {
  it("blocks while a turn is streaming", () => {
    expect(regenerateBlocked("streaming", false)).toBe(true);
  });
  it("blocks when no model is connected, so the destructive rewind never runs", () => {
    expect(regenerateBlocked("idle", true)).toBe(true);
    expect(regenerateBlocked("error", true)).toBe(true);
  });
  it("allows regenerate when idle with a model connected", () => {
    expect(regenerateBlocked("idle", false)).toBe(false);
    expect(regenerateBlocked("error", false)).toBe(false);
  });
});
