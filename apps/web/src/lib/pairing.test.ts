import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  claimPairingCode,
  describePairingError,
  formatPairingCode,
  isCompletePairingCode,
  redeemPairingFromUrl,
} from "./pairing";
import { QuotaStorage } from "./quotaStorage.testkit";
import { __resetStorageSeamForTests } from "./storage";

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
      vi.fn(async () => ({ ok: false, status: 400, json: async () => ({ error: "invalid or expired pairing code" }) })),
    );
    const res = await redeemPairingFromUrl();
    expect(res).toEqual({ ok: false, error: "HTTP 400: invalid or expired pairing code" });
    // A dead code in the address bar is pure leak — no upside to keeping it.
    expect(window.location.hash).toBe("");
    expect(window.localStorage.getItem("protoagent.authToken")).toBeNull();
  });

  it("does not store a token when the response omits one", async () => {
    setHash("#pair=abc123");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ok: true }) })),
    );
    const res = await redeemPairingFromUrl();
    expect(res).toEqual({ ok: false, error: "HTTP 200: pairing failed" });
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

// ADR 0114 D1: the device token is a credential — a write the browser refused must come back
// as a failed pairing, never `{ ok: true }` for a token this browser never kept.
describe("redeemPairingFromUrl on a full quota", () => {
  it("returns { ok: false } instead of reporting success", async () => {
    const full = new QuotaStorage(100);
    full.setItem("plugin.hog", "x".repeat(40));
    vi.stubGlobal("localStorage", full);
    setHash("#pair=abc123");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, json: async () => ({ ok: true, token: "device-token", device: { id: "d" } }) })),
    );
    const res = await redeemPairingFromUrl();
    expect(res?.ok).toBe(false);
    expect(res && !res.ok ? res.error : "").toMatch(/storage is full/);
    expect(full.getItem("protoagent.authToken")).toBeNull();
    vi.unstubAllGlobals();
    __resetStorageSeamForTests();
  });
});

// ADR 0087 D3 amendment: the same short code can be TYPED (AuthGate "Have a pairing code?").
describe("claimPairingCode + typed-code helpers", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("formats a typed code like the screen shows it", () => {
    expect(formatPairingCode("k7qm2xpa4f")).toBe("K7QM2-XPA4F");
    expect(formatPairingCode(" k7qm2 xpa4f ")).toBe("K7QM2-XPA4F");
    expect(formatPairingCode("k7qm2")).toBe("K7QM2"); // no dangling dash
    expect(isCompletePairingCode("K7QM2-XPA4")).toBe(false);
    expect(isCompletePairingCode("k7qm2-xpa4f")).toBe(true);
  });

  it("claims a typed code and stores the token under the same key a link uses", async () => {
    const fetchSpy = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, token: "typed-token", device: { id: "d2" } }),
    }));
    vi.stubGlobal("fetch", fetchSpy);
    expect(await claimPairingCode("K7QM2-XPA4F")).toEqual({ ok: true });
    expect(window.localStorage.getItem("protoagent.authToken")).toBe("typed-token");
    const body = JSON.parse(String((fetchSpy.mock.calls[0] as unknown as [string, RequestInit])[1].body));
    expect(body.code).toBe("K7QM2-XPA4F");
  });

  it("carries the HTTP status on a rejected claim", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 403, json: async () => ({ error: "invalid or expired pairing code" }) })),
    );
    const res = await claimPairingCode("ZZZZZ-ZZZZZ");
    expect(res).toEqual({ ok: false, status: 403, error: "HTTP 403: invalid or expired pairing code" });
  });

  it("a #pair= link with the short code still claims it verbatim", async () => {
    window.history.replaceState(null, "", "/app/#pair=K7QM2-XPA4F");
    const fetchSpy = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ ok: true, token: "t" }) }));
    vi.stubGlobal("fetch", fetchSpy);
    expect(await redeemPairingFromUrl()).toEqual({ ok: true });
    const body = JSON.parse(String((fetchSpy.mock.calls[0] as unknown as [string, RequestInit])[1].body));
    expect(body.code).toBe("K7QM2-XPA4F");
    expect(window.location.hash).toBe("");
  });
});

describe("describePairingError", () => {
  it("turns the one 403 into words naming every cause and the fix", () => {
    const msg = describePairingError({ status: 403, error: "HTTP 403: invalid or expired pairing code" });
    expect(msg).toMatch(/didn't work/);
    expect(msg).toMatch(/expired/);
    expect(msg).toMatch(/wrong tries/);
    expect(msg).toMatch(/Add a device/);
  });

  it("explains a network failure as reachability", () => {
    expect(describePairingError({ error: "Failed to fetch" })).toMatch(/Couldn't reach this agent/);
    expect(describePairingError({ error: "The operation was aborted due to timeout" })).toMatch(/Couldn't reach/);
  });

  it("passes a storage failure through untouched", () => {
    expect(describePairingError({ error: "browser storage is full" })).toBe("browser storage is full");
  });
});
