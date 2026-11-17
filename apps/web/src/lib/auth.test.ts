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

  it("saveAuthToken drops a paired-device token that would otherwise shadow it (#4336 S3)", () => {
    // The device token is sent first, so if the gate is up it is the one that 401'd.
    window.localStorage.setItem("protoagent.deviceToken", "revoked-device-token");
    window.localStorage.setItem("protoagent.deviceId", "ph1");
    notifyAuthRequired();
    expect(saveAuthToken("operator-token")).toEqual({ ok: true });
    expect(window.localStorage.getItem("protoagent.deviceToken")).toBeNull();
    expect(window.localStorage.getItem("protoagent.deviceId")).toBeNull();
    expect(window.localStorage.getItem("protoagent.authToken")).toBe("operator-token");
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

  it.each(["throws", "unavailable"])("does not confirm token removal when storage %s", (mode) => {
    const storage = new QuotaStorage(1000);
    storage.setItem("protoagent.authToken", "old");
    storage.removeItem = () => { throw new DOMException("Storage blocked", "SecurityError"); };
    vi.stubGlobal("localStorage", mode === "throws" ? storage : undefined);
    notifyAuthRequired();
    expect(saveAuthToken(" ").ok).toBe(false);
    expect(storage.getItem("protoagent.authToken")).toBe("old");
    expect(authRequired()).toBe(true);
  });

  it("reports success when it did save", () => {
    expect(saveAuthToken("t")).toEqual({ ok: true });
  });
});

// A 401 for a credential that was replaced while the request was in flight is stale (#5408):
// it must not put the gate back up over a console that is already signed in.
describe("stale 401s", () => {
  beforeEach(() => {
    window.localStorage.clear();
    clearAuthRequired();
  });

  it("ignores a 401 for a token that has since been replaced", () => {
    saveAuthToken("new-token");
    notifyAuthRequired("Bearer old-token");
    expect(authRequired()).toBe(false);
    notifyAuthRequired(null); // sent no credential, but one is stored now
    expect(authRequired()).toBe(false);
  });

  it("prompts for a 401 on the credential still stored, or with none stored", () => {
    saveAuthToken("cur");
    notifyAuthRequired("Bearer cur");
    expect(authRequired()).toBe(true);
    clearAuthRequired();
    saveAuthToken("");
    notifyAuthRequired(null);
    expect(authRequired()).toBe(true);
  });

  it("an untagged 401 (no header given) always counts", () => {
    saveAuthToken("cur");
    notifyAuthRequired();
    expect(authRequired()).toBe(true);
  });
});

// Desktop self-auth (#2055): the first 401 tries the shell's own token before prompting. Once
// that token is saved it must recover like the manual prompt does, refetching every query; a
// 401 never retries, so a panel that 401'd would otherwise stay errored (#5408).
describe("desktop self-auth", () => {
  afterEach(() => {
    vi.doUnmock("./desktop");
    vi.doUnmock("./queryClient");
    vi.resetModules();
  });

  async function load(shellToken: string) {
    vi.resetModules();
    window.localStorage.clear();
    const invalidateQueries = vi.fn(async () => {});
    vi.doMock("./desktop", () => ({ desktopAuthToken: async () => shellToken }));
    vi.doMock("./queryClient", () => ({ queryClient: { invalidateQueries } }));
    const auth = await import("./auth");
    return { auth, invalidateQueries };
  }

  it("saves the shell's token, clears the gate and refetches every query", async () => {
    const { auth, invalidateQueries } = await load("shell-token");
    auth.notifyAuthRequired();
    await vi.waitFor(() => expect(invalidateQueries).toHaveBeenCalledTimes(1));
    expect(window.localStorage.getItem("protoagent.authToken")).toBe("shell-token");
    expect(auth.authRequired()).toBe(false);
  });

  it("refetches nothing when the shell has no token to offer (the gate stays up)", async () => {
    const { auth, invalidateQueries } = await load("");
    auth.notifyAuthRequired();
    await new Promise((r) => setTimeout(r, 20));
    expect(invalidateQueries).not.toHaveBeenCalled();
    expect(auth.authRequired()).toBe(true);
  });
});
