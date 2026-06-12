import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  authRequired,
  clearAuthRequired,
  notifyAuthRequired,
  saveAuthToken,
  subscribeAuth,
} from "./auth";

// The 401-driven auth store (#873): request() trips it, the AuthGate dialog
// subscribes, saveAuthToken persists the bearer api.ts's authToken() reads.

describe("auth store", () => {
  beforeEach(() => {
    window.localStorage.clear();
    clearAuthRequired();
  });

  it("starts clear, flips on notify, clears on demand", () => {
    expect(authRequired()).toBe(false);
    notifyAuthRequired();
    expect(authRequired()).toBe(true);
    clearAuthRequired();
    expect(authRequired()).toBe(false);
  });

  it("notifies subscribers once per transition (bursts of 401s are idempotent)", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeAuth(listener);
    notifyAuthRequired();
    notifyAuthRequired();
    notifyAuthRequired();
    expect(listener).toHaveBeenCalledTimes(1);
    clearAuthRequired();
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
    notifyAuthRequired();
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("saveAuthToken writes the key authToken() reads and clears the prompt", () => {
    notifyAuthRequired();
    saveAuthToken("  secret-token  ");
    expect(window.localStorage.getItem("protoagent.authToken")).toBe("secret-token");
    expect(authRequired()).toBe(false);
  });

  it("saveAuthToken with a blank value removes the stored token", () => {
    window.localStorage.setItem("protoagent.authToken", "old");
    saveAuthToken("   ");
    expect(window.localStorage.getItem("protoagent.authToken")).toBeNull();
  });
});
