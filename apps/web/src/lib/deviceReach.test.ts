import { describe, expect, it } from "vitest";

import { reachKey, reachLabel, reachRequirement, waitForRestart } from "./deviceReach";

describe("reachLabel", () => {
  it("names each way in", () => {
    expect(reachLabel({ kind: "tailnet", via: "serve", host: "box.ts.net" })).toBe("Tailnet HTTPS · box.ts.net");
    expect(reachLabel({ kind: "tailnet", via: "magicdns", host: "box.ts.net" })).toBe("Tailnet name · box.ts.net");
    expect(reachLabel({ kind: "tailnet", via: "ip", host: "100.64.0.5" })).toBe("Tailnet · 100.64.0.5");
    expect(reachLabel({ kind: "tailnet", host: "100.64.0.5" })).toBe("Tailnet · 100.64.0.5"); // older server
    expect(reachLabel({ kind: "lan", host: "192.168.1.2" })).toBe("Wi-Fi · 192.168.1.2");
  });
});

describe("reachRequirement", () => {
  it("says a tailnet address needs Tailscale on the phone", () => {
    expect(reachRequirement({ kind: "tailnet", via: "ip" })).toMatch(/Tailscale app, signed in to the same tailnet/);
    expect(reachRequirement({ kind: "tailnet", via: "serve", secure: true })).toMatch(/HTTPS.*Tailscale app/);
  });
  it("says LAN is same Wi-Fi only", () => {
    expect(reachRequirement({ kind: "lan" })).toMatch(/^Same Wi-Fi only/);
  });
  it("warns loudly about Funnel", () => {
    expect(reachRequirement({ kind: "tailnet", via: "serve", secure: true, funnel: true })).toMatch(/public on the internet/);
  });
});

describe("reachKey", () => {
  it("keys by origin so https and http forms of one name don't collide", () => {
    expect(reachKey({ host: "b", origin: "https://b" })).not.toBe(reachKey({ host: "b", origin: "http://b:7870" }));
  });
});

describe("waitForRestart", () => {
  const sleep = () => Promise.resolve();
  it("waits for down then up", async () => {
    const seq = [true, false, false, true];
    let i = 0;
    expect(await waitForRestart(async () => seq[i++] ?? true, { sleep, intervalMs: 100, downMs: 10000 })).toBe(true);
    expect(i).toBe(4);
  });
  it("accepts a restart too fast to observe once downMs has passed", async () => {
    let calls = 0;
    expect(await waitForRestart(async () => (calls++, true), { sleep, intervalMs: 1000, downMs: 3000 })).toBe(true);
    expect(calls).toBe(3);
  });
  it("gives up after the timeout", async () => {
    expect(await waitForRestart(async () => false, { sleep, intervalMs: 1000, timeoutMs: 5000 })).toBe(false);
  });
});
