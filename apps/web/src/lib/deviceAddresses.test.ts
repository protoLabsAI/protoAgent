import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  completeDeviceMove,
  deviceAddresses,
  deviceMovedTo,
  markDeviceMoved,
  otherDeviceAddresses,
  parseDeviceAddresses,
  storeDeviceAddresses,
  storeDeviceCredential,
} from "./credentials";
import { addressLabel, goToHandoff, requestHandoff, requirementText } from "./deviceHandoff";
import { rotateDeviceTokenIfDue } from "./deviceRotation";
import { __resetStorageSeamForTests } from "./storage";

// ADR 0121 D1 (#4336 S7.5) — the address set a paired phone keeps beside its token, and the
// one-tap handoff to another of the agent's origins.

const DEV = "protoagent.deviceToken";
const ADDR = "protoagent.deviceAddresses";
const MOVED = "protoagent.deviceMovedTo";
const HOME = window.location.origin; // the test page's own origin plays the phone's home

const SET = [
  { kind: "tailnet_https", origin: "https://box.tail1234.ts.net", requirement: "tailscale" },
  { kind: "public", origin: "https://agent.example.com" },
  { kind: "lan", origin: HOME, requirement: "same_network" },
];

afterEach(() => {
  vi.unstubAllGlobals();
  __resetStorageSeamForTests();
});

describe("the stored address set", () => {
  beforeEach(() => window.localStorage.clear());

  it("keeps only well-formed origins of known kinds, in the server's order", () => {
    expect(
      parseDeviceAddresses([
        ...SET,
        { kind: "lan", origin: "javascript:alert(1)" },
        { kind: "lan", origin: "https://box.example/app/" }, // not an origin
        { kind: "lan", origin: "https://evil.example@box.example" },
        { kind: "warp", origin: "https://x.example" },
        { kind: "tailnet", origin: "http://100.64.0.5:7871", requirement: "a wish" },
        null,
        "https://x.example",
      ]),
    ).toEqual([...SET, { kind: "tailnet", origin: "http://100.64.0.5:7871" }]);
    expect(parseDeviceAddresses({ not: "a list" })).toEqual([]);
  });

  it("is stored next to the device token and read only while that token is here", () => {
    storeDeviceAddresses(SET);
    expect(deviceAddresses()).toEqual([]); // no device token: not a paired device
    storeDeviceCredential("tok", "d1");
    storeDeviceAddresses(SET);
    expect(deviceAddresses()).toEqual(SET);
    expect(otherDeviceAddresses().map((a) => a.origin)).toEqual(["https://box.tail1234.ts.net", "https://agent.example.com"]);
  });

  it("an answer with nothing usable leaves the stored set alone", () => {
    storeDeviceCredential("tok", "d1");
    storeDeviceAddresses(SET);
    storeDeviceAddresses(undefined);
    storeDeviceAddresses([]);
    expect(deviceAddresses()).toEqual(SET);
  });

  it("is forgotten with the device credential", () => {
    storeDeviceCredential("tok", "d1");
    storeDeviceAddresses(SET);
    completeDeviceMove();
    expect(window.localStorage.getItem(ADDR)).toBeNull();
    expect(window.localStorage.getItem(DEV)).toBeNull();
  });

  it("the 'moved to' pointer shows only once this origin holds no device token, and re-pairing here clears it", () => {
    storeDeviceCredential("tok", "d1");
    markDeviceMoved("https://box.tail1234.ts.net");
    expect(deviceMovedTo()).toBe(""); // still this phone's home until the move completes
    completeDeviceMove();
    expect(deviceMovedTo()).toBe("https://box.tail1234.ts.net");
    storeDeviceCredential("tok2", "d1");
    expect(window.localStorage.getItem(MOVED)).toBeNull();
  });

  it("words each address and what it needs", () => {
    expect(SET.map((a) => `${addressLabel(a as never)}: ${requirementText(a as never)}`)).toEqual([
      "Tailnet HTTPS: needs the Tailscale app on this phone",
      "Public URL: works from any network",
      "Wi-Fi: only on the same Wi-Fi as the agent",
    ]);
  });
});

describe("the boot /self call refreshes the set and finishes a move", () => {
  beforeEach(() => window.localStorage.clear());

  function serve(body: unknown) {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })));
  }

  it("stores the addresses /api/devices/self reports", async () => {
    storeDeviceCredential("tok", "d1");
    serve({ device: { id: "d1", name: "phone", rotate_due: false, created_at: 1, last_seen_at: null }, addresses: SET });
    expect(await rotateDeviceTokenIfDue()).toBe("not-due");
    expect(deviceAddresses()).toEqual(SET);
  });

  it("after the grace, a 401 on an origin that recorded a move forgets the token too", async () => {
    storeDeviceCredential("tok", "d1");
    markDeviceMoved("https://box.tail1234.ts.net");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    expect(await rotateDeviceTokenIfDue()).toBe("moved");
    expect(window.localStorage.getItem(DEV)).toBeNull();
    expect(deviceMovedTo()).toBe("https://box.tail1234.ts.net");
  });

  it("a move that never completed leaves the token and drops the pointer", async () => {
    storeDeviceCredential("tok", "d1");
    markDeviceMoved("https://box.tail1234.ts.net");
    serve({ device: { id: "d1", name: "phone", rotate_due: false, handed_off: false, created_at: 1, last_seen_at: null } });
    expect(await rotateDeviceTokenIfDue()).toBe("not-due");
    expect(window.localStorage.getItem(DEV)).toBe("tok");
    expect(window.localStorage.getItem(MOVED)).toBeNull();
  });

  it("a 401 without a recorded move is just an error", async () => {
    storeDeviceCredential("tok", "d1");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    expect(await rotateDeviceTokenIfDue()).toBe("error");
    expect(window.localStorage.getItem(DEV)).toBe("tok");
  });

  it("a handed-off token is forgotten here, so it is never presented again", async () => {
    storeDeviceCredential("tok", "d1");
    markDeviceMoved("https://box.tail1234.ts.net");
    serve({ device: { id: "d1", name: "phone", rotate_due: true, handed_off: true, created_at: 1, last_seen_at: null } });
    expect(await rotateDeviceTokenIfDue()).toBe("moved");
    expect(window.localStorage.getItem(DEV)).toBeNull();
    expect(deviceMovedTo()).toBe("https://box.tail1234.ts.net");
  });
});

describe("requestHandoff", () => {
  beforeEach(() => window.localStorage.clear());

  it("posts the origin with the device token and returns the server's URL", async () => {
    storeDeviceCredential("tok", "d1");
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ ok: true, code: "K7QM2-XPA4F", url: "https://box.tail1234.ts.net/app/#pair=K7QM2-XPA4F" })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const res = await requestHandoff("https://box.tail1234.ts.net");
    expect(res).toEqual({ ok: true, url: "https://box.tail1234.ts.net/app/#pair=K7QM2-XPA4F" });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/api\/devices\/self\/handoff$/);
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok");
    expect(JSON.parse(String(init.body))).toEqual({ origin: "https://box.tail1234.ts.net" });
  });

  it("refuses a URL that isn't on the origin it asked for", async () => {
    storeDeviceCredential("tok", "d1");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ok: true, url: "https://evil.example/app/#pair=X" }))));
    expect((await requestHandoff("https://box.tail1234.ts.net")).ok).toBe(false);
  });

  it("surfaces the server's refusal as a sentence", async () => {
    storeDeviceCredential("tok", "d1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ error: "origin_not_offered", detail: "that address isn't one this agent offers" }), { status: 400 })),
    );
    expect(await requestHandoff("https://x.example")).toEqual({ ok: false, error: "That address isn't one this agent offers." });
  });

  it("asks nothing without a device token", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect((await requestHandoff("https://x.example")).ok).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("goToHandoff records where the phone went, then navigates", () => {
    const go = vi.fn();
    goToHandoff("https://box.tail1234.ts.net", "https://box.tail1234.ts.net/app/#pair=K", go);
    expect(window.localStorage.getItem(MOVED)).toBe("https://box.tail1234.ts.net");
    expect(go).toHaveBeenCalledWith("https://box.tail1234.ts.net/app/#pair=K");
  });
});
