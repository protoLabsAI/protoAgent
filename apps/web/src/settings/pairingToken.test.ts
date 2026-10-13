import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QuotaStorage } from "../lib/quotaStorage.testkit";
import { __resetStorageSeamForTests } from "../lib/storage";
import { PairingTokenRecoveryError, savePairingToken } from "./pairingToken";

const key = "protoagent.authToken";
let storage: QuotaStorage;
beforeEach(() => {
  storage = new QuotaStorage(1000);
  vi.stubGlobal("localStorage", storage);
});
afterEach(() => {
  vi.unstubAllGlobals();
  __resetStorageSeamForTests();
});
describe("pairing token persistence", () => {
  it("never updates the server when the browser cannot keep the token", async () => {
    storage.quota = 0;
    const save = vi.fn();
    await expect(savePairingToken("new", save)).rejects.toThrow(/storage is full/);
    expect(save).not.toHaveBeenCalled();
  });
  it("persists before the server accepts live auth", async () => {
    await savePairingToken("new", async () => {
      expect(storage.getItem(key)).toBe("new");
      return { ok: true, messages: [] };
    });
    expect(storage.getItem(key)).toBe("new");
  });
  it.each([null, "previous"])("restores %s after a confirmed rejection", async (prior) => {
    if (prior !== null) storage.setItem(key, prior);
    await expect(savePairingToken("new", async () => ({ ok: false, messages: ["denied"] }))).rejects.toThrow("denied");
    expect(storage.getItem(key)).toBe(prior);
  });
  it("preserves a longer previous credential for recovery if rollback runs out of space", async () => {
    const prior = "p".repeat(200);
    storage.setItem(key, prior);
    const failure = await savePairingToken("new", async () => {
      storage.quota = storage.used();
      return { ok: false, messages: ["denied"] };
    }).catch((err: unknown) => err);
    expect(failure).toBeInstanceOf(PairingTokenRecoveryError);
    expect((failure as PairingTokenRecoveryError).priorToken).toBe(prior);
    expect((failure as Error).message).not.toContain(prior);
  });
  it("reports a failed rollback removal", async () => {
    await expect(savePairingToken("new", async () => {
      storage.removeItem = () => { throw new Error("blocked"); };
      return { ok: false, messages: ["denied"] };
    })).rejects.toBeInstanceOf(PairingTokenRecoveryError);
  });
  it("keeps the new token when a lost response leaves the server outcome unknown", async () => {
    await expect(savePairingToken("new", async () => { throw new Error("offline"); })).rejects.toThrow("offline");
    expect(storage.getItem(key)).toBe("new");
  });
});
