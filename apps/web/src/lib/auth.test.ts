import { afterEach, describe, it, expect, beforeEach, vi } from "vitest";
import {
  authRequired,
  clearAuthRequired,
  notifyAuthRequired,
  saveAuthToken,
  subscribeAuth,
} from "./auth";
import { QuotaStorage } from "./quotaStorage.testkit";
import { __resetStorageSeamForTests } from "./storage";

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

// ADR 0114 D1: a credential write is STRICT — a token the browser couldn't keep must not
// report success (every request would still go out without it) nor clear the prompt.
describe("saveAuthToken on a full quota", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    __resetStorageSeamForTests();
    clearAuthRequired();
  });

  it("fails loudly and leaves the prompt up", () => {
    const full = new QuotaStorage(100);
    full.setItem("plugin.hog", "x".repeat(40)); // 100 bytes: nothing else fits
    vi.stubGlobal("localStorage", full);
    notifyAuthRequired();
    const res = saveAuthToken("secret-token");
    expect(res.ok).toBe(false);
    expect(res.ok ? "" : res.error).toMatch(/storage is full/);
    expect(full.getItem("protoagent.authToken")).toBeNull();
    expect(authRequired()).toBe(true);
  });

  it("reports success when it did save", () => {
    expect(saveAuthToken("t")).toEqual({ ok: true });
  });
});
