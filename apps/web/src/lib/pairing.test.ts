import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  claimPairingCode,
  describePairingError,
  formatPairingCode,
  isCompletePairingCode,
  redeemPairingFromUrl,
  retryAfterSeconds,
  subscribePaired,
  takePairedNotice,
} from "./pairing";
import { authToken } from "./api/routing";
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
    // Its OWN key (#4336 S3) — the operator key is left alone.
    expect(window.localStorage.getItem("protoagent.deviceToken")).toBe("device-token");
    expect(window.localStorage.getItem("protoagent.authToken")).toBeNull();
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

  it("a throttled QR claim (429) reads as a wait, with Retry-After", async () => {
    setHash("#pair=K7QM2-XPRT4");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 429,
        headers: new Headers({ "Retry-After": "60" }),
        json: async () => ({ ok: false, error: "too many pairing attempts — try again in 60 s", retry_after: 60 }),
      })),
    );
    const res = await redeemPairingFromUrl();
    expect(res).toEqual({ ok: false, error: expect.stringMatching(/^Too many attempts — try again in 60 s\./) });
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
    expect(window.localStorage.getItem("protoagent.deviceToken")).toBeNull();
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
    expect(full.getItem("protoagent.deviceToken")).toBeNull();
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
    expect(window.localStorage.getItem("protoagent.deviceToken")).toBe("typed-token");
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

// #4336 S3 — the token's own key, the claim's URL, and the phone-side confirmation.
describe("claimPairingCode (#4336 S3)", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/app/");
    takePairedNotice();
  });

  const ok = (device: object = { id: "d9", name: "Josh's iPhone" }) =>
    vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, token: "dev-tok", device }),
    }));

  it("never overwrites an operator token already in this browser", async () => {
    window.localStorage.setItem("protoagent.authToken", "operator-secret");
    vi.stubGlobal("fetch", ok());
    expect(await claimPairingCode("K7QM2-XPA4F")).toEqual({ ok: true });
    expect(window.localStorage.getItem("protoagent.authToken")).toBe("operator-secret");
    expect(window.localStorage.getItem("protoagent.deviceToken")).toBe("dev-tok");
    // …and the device token is the one sent from now on.
    expect(authToken()).toBe("dev-tok");
  });

  it("claims at the origin-root /api for the host console", async () => {
    const fetchSpy = ok();
    vi.stubGlobal("fetch", fetchSpy);
    await claimPairingCode("K7QM2-XPA4F");
    expect(fetchSpy.mock.calls[0][0]).toBe("/api/pairing/claim");
  });

  it("claims through the hub's per-agent prefix when the console is served under one", async () => {
    window.history.replaceState(null, "", "/app/agent/ava/");
    const fetchSpy = ok();
    vi.stubGlobal("fetch", fetchSpy);
    await claimPairingCode("K7QM2-XPA4F");
    expect(fetchSpy.mock.calls[0][0]).toBe("/agents/ava/api/pairing/claim");
  });

  it("claims against an explicit API base", async () => {
    window.localStorage.setItem("protoagent.apiBase", "http://10.0.0.5:7870/");
    const fetchSpy = ok();
    vi.stubGlobal("fetch", fetchSpy);
    await claimPairingCode("K7QM2-XPA4F");
    expect(fetchSpy.mock.calls[0][0]).toBe("http://10.0.0.5:7870/api/pairing/claim");
  });

  it("leaves a one-shot 'paired as' notice with the server's name for the device", async () => {
    const seen = vi.fn();
    const off = subscribePaired(seen);
    vi.stubGlobal("fetch", ok());
    await claimPairingCode("K7QM2-XPA4F");
    off();
    expect(seen).toHaveBeenCalledTimes(1);
    expect(takePairedNotice()).toEqual({ id: "d9", name: "Josh's iPhone" });
    expect(takePairedNotice()).toBeNull(); // consumed — one claim confirms once
  });

  it("a failed claim leaves no notice", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 403, json: async () => ({}) })));
    await claimPairingCode("ZZZZZ-ZZZZZ");
    expect(takePairedNotice()).toBeNull();
  });

  it("a re-pair drops the previous device id before storing the new token", async () => {
    window.localStorage.setItem("protoagent.deviceId", "old-id");
    vi.stubGlobal("fetch", ok({ name: "iPhone" })); // no id in the answer
    await claimPairingCode("K7QM2-XPA4F");
    // An old id must never be paired with the NEW token — it would mark the wrong row.
    expect(window.localStorage.getItem("protoagent.deviceId")).toBeNull();
  });
});

describe("retryAfterSeconds", () => {
  it("reads the header, falls back to the body, and ignores junk", () => {
    expect(retryAfterSeconds("60", 5)).toBe(60);
    expect(retryAfterSeconds(null, 12)).toBe(12);
    expect(retryAfterSeconds("Wed, 21 Oct 2026 07:28:00 GMT", undefined)).toBeUndefined();
    expect(retryAfterSeconds("0", "x")).toBeUndefined();
  });
});

describe("describePairingError", () => {
  it("turns the one 403 into words naming every cause and the fix", () => {
    const msg = describePairingError({ status: 403, error: "HTTP 403: invalid or expired pairing code" });
    expect(msg).toMatch(/didn't work/);
    expect(msg).toMatch(/expired/);
    expect(msg).toMatch(/Add a device/);
    // Wrong guesses no longer cancel codes (ADR 0121 D3), so the message mustn't claim they do.
    expect(msg).not.toMatch(/wrong tries|cancelled/);
  });

  it("turns a 429 into a wait, with the server's Retry-After", () => {
    const msg = describePairingError({ status: 429, error: "HTTP 429: too many", retryAfter: 37 });
    expect(msg).toMatch(/^Too many attempts — try again in 37 s\./);
    expect(msg).not.toMatch(/didn't work|invalid/);
    expect(describePairingError({ status: 429, error: "HTTP 429" })).toMatch(/try again in a minute/);
  });

  it("explains a network failure as reachability", () => {
    expect(describePairingError({ error: "Failed to fetch" })).toMatch(/Couldn't reach this agent/);
    // All three routes: Wi-Fi, the tailnet, and the operator's own public URL (ADR 0121 D2).
    expect(describePairingError({ error: "Failed to fetch" })).toMatch(/Wi-Fi or tailnet, or open the agent's public address/);
    expect(describePairingError({ error: "The operation was aborted due to timeout" })).toMatch(/Couldn't reach/);
  });

  it("passes a storage failure through untouched", () => {
    expect(describePairingError({ error: "browser storage is full" })).toBe("browser storage is full");
  });
});
