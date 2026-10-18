import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  currentDeviceId,
  effectiveToken,
  forgetDeviceCredential,
  migrateLegacyDeviceToken,
  storeDeviceCredential,
} from "./credentials";
import { QuotaStorage } from "./quotaStorage.testkit";
import { __resetStorageSeamForTests } from "./storage";

// #4336 S3 — the device token has its own key; the operator token keeps its own.

const OP = "protoagent.authToken";
const DEV = "protoagent.deviceToken";
const ID = "protoagent.deviceId";

describe("credentials", () => {
  beforeEach(() => window.localStorage.clear());

  it("sends the device token when paired, else the operator token, else nothing", () => {
    expect(effectiveToken()).toBe("");
    window.localStorage.setItem(OP, "op");
    expect(effectiveToken()).toBe("op");
    window.localStorage.setItem(DEV, "dev");
    expect(effectiveToken()).toBe("dev");
  });

  it("knows its device id only while it holds a device token", () => {
    window.localStorage.setItem(ID, "d1");
    expect(currentDeviceId()).toBe(""); // a stale id names nothing
    storeDeviceCredential("tok", "d1");
    expect(currentDeviceId()).toBe("d1");
  });

  it("forgetting the device falls back to a kept operator token", () => {
    window.localStorage.setItem(OP, "op");
    storeDeviceCredential("dev", "d1");
    expect(forgetDeviceCredential()).toBe(true); // still signed in
    expect(effectiveToken()).toBe("op");
    expect(window.localStorage.getItem(ID)).toBeNull();
  });

  it("forgetting the only credential reports signed out", () => {
    storeDeviceCredential("dev", "d1");
    expect(forgetDeviceCredential()).toBe(false);
    expect(effectiveToken()).toBe("");
  });
});

describe("migrateLegacyDeviceToken", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => {
    vi.unstubAllGlobals();
    __resetStorageSeamForTests();
  });

  it("moves a pre-S3 phone's token to its own key without changing what's sent", () => {
    window.localStorage.setItem(OP, "legacy-device-token");
    window.localStorage.setItem(ID, "ph1");
    const before = effectiveToken();
    expect(migrateLegacyDeviceToken()).toBe("migrated");
    expect(window.localStorage.getItem(DEV)).toBe("legacy-device-token");
    expect(window.localStorage.getItem(OP)).toBeNull();
    expect(effectiveToken()).toBe(before); // nobody signed out
    expect(currentDeviceId()).toBe("ph1");
  });

  it("is idempotent", () => {
    window.localStorage.setItem(OP, "t");
    window.localStorage.setItem(ID, "ph1");
    migrateLegacyDeviceToken();
    expect(migrateLegacyDeviceToken()).toBe("nothing-to-do");
    expect(window.localStorage.getItem(DEV)).toBe("t");
  });

  it("leaves an operator-only browser alone", () => {
    window.localStorage.setItem(OP, "op");
    expect(migrateLegacyDeviceToken()).toBe("nothing-to-do");
    expect(window.localStorage.getItem(OP)).toBe("op");
    expect(window.localStorage.getItem(DEV)).toBeNull();
  });

  it("never clobbers a device token that's already where it belongs", () => {
    window.localStorage.setItem(OP, "op");
    window.localStorage.setItem(ID, "ph1");
    window.localStorage.setItem(DEV, "dev");
    expect(migrateLegacyDeviceToken()).toBe("nothing-to-do");
    expect(window.localStorage.getItem(OP)).toBe("op");
    expect(window.localStorage.getItem(DEV)).toBe("dev");
  });

  it("keeps the legacy key (still signed in) when the browser can't store the copy", () => {
    // 140 bytes of the 200 used; the 100-byte copy can't fit.
    const full = new QuotaStorage(200);
    full.setItem(OP, "legacy-device-token-xxxxxxxx");
    full.setItem(ID, "ph1");
    vi.stubGlobal("localStorage", full);
    __resetStorageSeamForTests();
    expect(migrateLegacyDeviceToken()).toBe("kept-legacy");
    expect(full.getItem(OP)).toBe("legacy-device-token-xxxxxxxx");
    expect(effectiveToken()).toBe("legacy-device-token-xxxxxxxx");
  });
});
