import { beforeEach, describe, expect, it, vi } from "vitest";

import { redeemPairingFromUrl } from "./pairing";

// Device pairing redemption (ADR 0087). The security-relevant behaviour here is that the
// code leaves the URL and history REGARDLESS of outcome — a spent or rejected code sitting
// in the address bar leaks into screenshots and back-button history for no benefit.

function setHash(hash: string) {
  window.history.replaceState(null, "", `/app/${hash}`);
}

describe("redeemPairingFromUrl", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setHash("");
    vi.restoreAllMocks();
  });

  it("does nothing (and makes no request) without a pair fragment", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    expect(await redeemPairingFromUrl()).toBeNull();
    // The normal boot path must not pay for a network round trip.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("claims the code and stores the returned token", async () => {
    setHash("#pair=abc123");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ ok: true, token: "device-token", device: { id: "dev1" } }),
      })),
    );
    expect(await redeemPairingFromUrl()).toEqual({ ok: true });
    expect(window.localStorage.getItem("protoagent.authToken")).toBe("device-token");
    expect(window.localStorage.getItem("protoagent.deviceId")).toBe("dev1");
  });

  it("strips the code from the URL even when the claim SUCCEEDS", async () => {
    setHash("#pair=abc123");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, json: async () => ({ ok: true, token: "t" }) })),
    );
    await redeemPairingFromUrl();
    expect(window.location.hash).toBe("");
  });

  it("strips the code from the URL even when the claim FAILS", async () => {
    setHash("#pair=expired");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, json: async () => ({ error: "invalid or expired pairing code" }) })),
    );
    const res = await redeemPairingFromUrl();
    expect(res).toEqual({ ok: false, error: "invalid or expired pairing code" });
    // A dead code in the address bar is pure leak — no upside to keeping it.
    expect(window.location.hash).toBe("");
    expect(window.localStorage.getItem("protoagent.authToken")).toBeNull();
  });

  it("does not store a token when the response omits one", async () => {
    setHash("#pair=abc123");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, json: async () => ({ ok: true }) })),
    );
    const res = await redeemPairingFromUrl();
    expect(res).toEqual({ ok: false, error: "pairing failed" });
    expect(window.localStorage.getItem("protoagent.authToken")).toBeNull();
  });

  it("surfaces a network failure instead of throwing into boot", async () => {
    setHash("#pair=abc123");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("offline");
      }),
    );
    // main.tsx renders in `.finally`, but a rejected promise here would still be an
    // unhandled rejection on every failed pairing.
    expect(await redeemPairingFromUrl()).toEqual({ ok: false, error: "offline" });
  });

  it("ignores a fragment that isn't a pairing code", async () => {
    setHash("#settings");
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    expect(await redeemPairingFromUrl()).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(window.location.hash).toBe("#settings"); // untouched — not ours to strip
  });
});
