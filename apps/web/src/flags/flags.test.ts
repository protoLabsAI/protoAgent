// @vitest-environment node
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

// ── the panel's toggle is stored on the agent, so server-gated flags really turn on ──
import { afterEach, vi } from "vitest";

import { api } from "../lib/api";
import { ApiError } from "../lib/api/http";
import { queryKeys } from "../lib/queries";
import { queryClient } from "../lib/queryClient";
import type { FlagsPayload } from "../lib/types";
import { resetAllFlags, resetFlag, saveFlag } from "./flags";

const payload = (enabled: boolean, source: "channel" | "override"): FlagsPayload => ({
  channel: "dev",
  flags: [{ id: "home_conversation", description: "Home", tier: "off", owner: "", remove_by: "", enabled, source, default: false }],
});

describe("saving a flag on the agent", () => {
  beforeEach(() => {
    resetFlagOverrides();
    queryClient.clear();
  });
  afterEach(() => vi.restoreAllMocks());

  it("stores the toggle on the agent and uses the list it returns", async () => {
    const set = vi.spyOn(api, "setFlag").mockResolvedValue(payload(true, "override"));
    const refetch = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue();
    setFlagOverride("home_conversation", false); // a stale device-local toggle
    expect(await saveFlag("home_conversation", true)).toBe("agent");
    expect(set).toHaveBeenCalledWith("home_conversation", true);
    expect(queryClient.getQueryData<FlagsPayload>(queryKeys.flags)?.flags[0].source).toBe("override");
    // The device-local stand-in must not mask what the agent now says.
    expect(useFlagOverrides.getState().overrides).toEqual({});
    // Data gated by the flag may exist now, so everything is refetched.
    expect(refetch).toHaveBeenCalled();
  });

  it("keeps the toggle on this device when the agent is too old to store it", async () => {
    vi.spyOn(api, "setFlag").mockRejectedValue(new ApiError(405, "Method Not Allowed"));
    expect(await saveFlag("home_conversation", true)).toBe("device");
    expect(useFlagOverrides.getState().overrides).toEqual({ home_conversation: true });
  });

  it("surfaces the agent's refusal and changes nothing", async () => {
    vi.spyOn(api, "setFlag").mockRejectedValue(new ApiError(409, "home_conversation is set by the PROTOAGENT_FLAG_HOME_CONVERSATION environment variable"));
    await expect(saveFlag("home_conversation", false)).rejects.toThrow(/environment variable/);
    expect(useFlagOverrides.getState().overrides).toEqual({});
    // An unknown flag is the agent's 404, not "too old": it must not fall back to the device.
    vi.spyOn(api, "setFlag").mockRejectedValue(new ApiError(404, "no developer flag named 'x'"));
    await expect(saveFlag("x", true)).rejects.toThrow(/no developer flag/);
    expect(useFlagOverrides.getState().overrides).toEqual({});
  });

  it("reset clears both the agent's override and this device's", async () => {
    const clear = vi.spyOn(api, "clearFlag").mockResolvedValue(payload(false, "channel"));
    vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue();
    setFlagOverride("home_conversation", true);
    await resetFlag("home_conversation");
    expect(clear).toHaveBeenCalledWith("home_conversation");
    expect(useFlagOverrides.getState().overrides).toEqual({});
    setFlagOverride("a", true);
    await resetAllFlags();
    expect(clear).toHaveBeenLastCalledWith();
    expect(useFlagOverrides.getState().overrides).toEqual({});
  });

  it("reset on an old agent still clears this device's toggle", async () => {
    vi.spyOn(api, "clearFlag").mockRejectedValue(new ApiError(405, "Method Not Allowed"));
    setFlagOverride("home_conversation", true);
    await resetFlag("home_conversation");
    expect(useFlagOverrides.getState().overrides).toEqual({});
  });
});

describe("a toggle left in this browser from before the agent stored them", () => {
  it("is ignored once the agent stores overrides, and still used against an older agent", async () => {
    const { agentStoresFlags } = await import("./flags");
    expect(agentStoresFlags(payload(false, "channel"))).toBe(true);
    const old: FlagsPayload = { channel: "dev", flags: [{ id: "x", description: "", tier: "off", owner: "", remove_by: "", enabled: false, source: "channel" }] };
    expect(agentStoresFlags(old)).toBe(false);
    expect(agentStoresFlags(undefined)).toBe(false);
  });
});
