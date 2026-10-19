import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { effectiveToken, replaceDeviceToken } from "./credentials";
import { rotateDeviceTokenIfDue } from "./deviceRotation";
import { QuotaStorage } from "./quotaStorage.testkit";
import { __resetStorageSeamForTests } from "./storage";

// ADR 0121 D5 (#4336 S7.3) — a paired phone rotates its token weekly, strictly.

const DEV = "protoagent.deviceToken";
const OP = "protoagent.authToken";

type Call = { method: string; url: string; auth: string | null };

function mockServer(self: unknown, rotate: { status: number; body: unknown } = { status: 200, body: { ok: true, token: "new-tok" } }) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
    const headers = new Headers(init.headers);
    calls.push({ method: init.method || "GET", url: String(url), auth: headers.get("Authorization") });
    if (String(url).endsWith("/api/devices/self/rotate")) {
      return new Response(JSON.stringify(rotate.body), { status: rotate.status });
    }
    if (String(url).endsWith("/api/devices/self")) {
      if (self === 404) return new Response("not found", { status: 404 });
      return new Response(JSON.stringify(self), { status: 200 });
    }
    return new Response("{}", { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const due = { device: { id: "d1", name: "phone", scope: "device", rotate_due: true, created_at: 1, last_seen_at: null } };
const notDue = { device: { ...due.device, rotate_due: false } };

describe("rotateDeviceTokenIfDue", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => {
    vi.unstubAllGlobals();
    __resetStorageSeamForTests();
  });

  it("does nothing — not even a request — without a device token", async () => {
    window.localStorage.setItem(OP, "op");
    const calls = mockServer(due);
    expect(await rotateDeviceTokenIfDue()).toBe("not-a-device");
    expect(calls).toEqual([]);
  });

  it("asks /self with the device token and stops when rotation isn't due", async () => {
    window.localStorage.setItem(DEV, "old-tok");
    const calls = mockServer(notDue);
    expect(await rotateDeviceTokenIfDue()).toBe("not-due");
    expect(calls).toEqual([{ method: "GET", url: "/api/devices/self", auth: "Bearer old-tok" }]);
  });

  it("rotates when due, stores strictly, and the next request carries the new token", async () => {
    window.localStorage.setItem(DEV, "old-tok");
    const calls = mockServer(due);
    expect(await rotateDeviceTokenIfDue()).toBe("rotated");
    expect(calls.map((c) => [c.method, c.url, c.auth])).toEqual([
      ["GET", "/api/devices/self", "Bearer old-tok"],
      ["POST", "/api/devices/self/rotate", "Bearer old-tok"],
    ]);
    expect(window.localStorage.getItem(DEV)).toBe("new-tok");
    expect(effectiveToken()).toBe("new-tok");
  });

  it("keeps the old token when the strict write fails", async () => {
    // Room for the old token, not for one twice its length.
    const store = new QuotaStorage(2 * (DEV.length + 8) + 4);
    store.setItem(DEV, "old-tok1");
    vi.stubGlobal("localStorage", store);
    __resetStorageSeamForTests();
    mockServer(due, { status: 200, body: { ok: true, token: "a-much-longer-rotated-token" } });
    expect(await rotateDeviceTokenIfDue()).toBe("store-failed");
    expect(store.getItem(DEV)).toBe("old-tok1");
    expect(effectiveToken()).toBe("old-tok1");
  });

  it("a 409 (another tab is rotating) leaves storage alone", async () => {
    window.localStorage.setItem(DEV, "old-tok");
    mockServer(due, { status: 409, body: { ok: false, error: "rotation_in_progress" } });
    expect(await rotateDeviceTokenIfDue()).toBe("busy");
    expect(window.localStorage.getItem(DEV)).toBe("old-tok");
  });

  it("an older server without /self is left alone", async () => {
    window.localStorage.setItem(DEV, "old-tok");
    mockServer(404);
    expect(await rotateDeviceTokenIfDue()).toBe("unsupported");
  });

  it("the shared bearer stored as a device token is told it isn't a device", async () => {
    window.localStorage.setItem(DEV, "old-tok");
    mockServer({ device: null });
    expect(await rotateDeviceTokenIfDue()).toBe("not-a-device");
  });

  it("concurrent calls share one rotation", async () => {
    window.localStorage.setItem(DEV, "old-tok");
    const calls = mockServer(due);
    const [a, b] = await Promise.all([rotateDeviceTokenIfDue(), rotateDeviceTokenIfDue()]);
    expect([a, b]).toEqual(["rotated", "rotated"]);
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("a second rotation in a row starts from the stored (new) token", async () => {
    window.localStorage.setItem(DEV, "old-tok");
    const calls = mockServer(due);
    await rotateDeviceTokenIfDue();
    const second = mockServer(due, { status: 200, body: { ok: true, token: "newer-tok" } });
    expect(await rotateDeviceTokenIfDue()).toBe("rotated");
    expect(calls).toHaveLength(2);
    expect(second.every((c) => c.auth === "Bearer new-tok")).toBe(true);
    expect(window.localStorage.getItem(DEV)).toBe("newer-tok");
  });

  it("never throws: a network failure is an error outcome", async () => {
    window.localStorage.setItem(DEV, "old-tok");
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("offline"))));
    expect(await rotateDeviceTokenIfDue()).toBe("error");
    expect(window.localStorage.getItem(DEV)).toBe("old-tok");
  });
});

describe("replaceDeviceToken", () => {
  beforeEach(() => window.localStorage.clear());

  it("writes only over the token it replaces", () => {
    window.localStorage.setItem(DEV, "t1");
    expect(replaceDeviceToken("t0", "t2")).toBe("changed");
    expect(window.localStorage.getItem(DEV)).toBe("t1");
    expect(replaceDeviceToken("t1", "t2")).toBe("replaced");
    expect(window.localStorage.getItem(DEV)).toBe("t2");
  });
});
