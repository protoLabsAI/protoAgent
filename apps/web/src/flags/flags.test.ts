import { beforeEach, describe, expect, it } from "vitest";

import { clearFlagOverride, resetFlagOverrides, setFlagOverride, useFlagOverrides } from "./flags";

// The device-local override store (ADR 0068) — the Developer panel's toggles. The useFlag /
// channel hooks are exercised end-to-end in e2e/developer-flags.spec.ts (they need the query).

describe("flag overrides store", () => {
  beforeEach(() => resetFlagOverrides());

  it("sets, clears, and resets device-local overrides", () => {
    expect(useFlagOverrides.getState().overrides).toEqual({});

    setFlagOverride("chat.new", true);
    setFlagOverride("chat.old", false);
    expect(useFlagOverrides.getState().overrides).toEqual({ "chat.new": true, "chat.old": false });

    clearFlagOverride("chat.new");
    expect(useFlagOverrides.getState().overrides).toEqual({ "chat.old": false });

    resetFlagOverrides();
    expect(useFlagOverrides.getState().overrides).toEqual({});
  });

  it("re-setting an override replaces its value", () => {
    setFlagOverride("x.y", true);
    setFlagOverride("x.y", false);
    expect(useFlagOverrides.getState().overrides["x.y"]).toBe(false);
  });
});
