import { describe, expect, it } from "vitest";

import {
  cancelBody,
  delegateLinkFor,
  formatAgentCode,
  formatCountdown,
  hostKindLabel,
  hubUrlRefusal,
  isCompleteAgentCode,
  isInsecureRefusal,
  needsInsecureOptIn,
  normalizeAgentCode,
  remoteAuthBadge,
  secondsLeft,
  tailnetFirst,
  transportSecurity,
} from "./agentPairing";
import devicesSrc from "../settings/DevicesPanel.tsx?raw";
import type { FleetAgent } from "./types";

// Agent pairing (ADR 0113) — the pure rules behind Settings ▸ Devices ▸ Pair an agent and the
// Fleet panel's Pair… / Re-pair dialog.

describe("agent code input", () => {
  it("upper-cases and tolerates dashes and spaces", () => {
    expect(normalizeAgentCode("abcde-fghij")).toBe("ABCDEFGHIJ");
    expect(normalizeAgentCode(" abcde fghij ")).toBe("ABCDEFGHIJ");
    expect(normalizeAgentCode("ab-cd e_fg.hij")).toBe("ABCDEFGHIJ");
  });

  it("caps at 10 characters (a paste with trailing junk doesn't overflow)", () => {
    expect(normalizeAgentCode("ABCDEFGHIJKLMN")).toBe("ABCDEFGHIJ");
  });

  it("leaves Crockford aliases to the server rather than rewriting what was typed", () => {
    expect(normalizeAgentCode("o0il1")).toBe("O0IL1");
  });

  it("formats XXXXX-XXXXX as typed, with no dangling dash", () => {
    expect(formatAgentCode("")).toBe("");
    expect(formatAgentCode("abc")).toBe("ABC");
    expect(formatAgentCode("abcde")).toBe("ABCDE");
    expect(formatAgentCode("abcdef")).toBe("ABCDE-F");
    expect(formatAgentCode("abcdefghij")).toBe("ABCDE-FGHIJ");
    // Idempotent: re-formatting the formatted value (every keystroke does) is stable.
    expect(formatAgentCode(formatAgentCode("abcde fghij"))).toBe("ABCDE-FGHIJ");
  });

  it("is complete only at 10 significant characters", () => {
    expect(isCompleteAgentCode("ABCDE-FGHI")).toBe(false);
    expect(isCompleteAgentCode("abcde fghij")).toBe(true);
    expect(isCompleteAgentCode("ABCDE-FGHIJ")).toBe(true);
  });
});

describe("countdown", () => {
  it("counts whole seconds to the server's expiry, never negative", () => {
    expect(secondsLeft(1000, 700_000)).toBe(300);
    expect(secondsLeft(1000, 999_600)).toBe(0);
    expect(secondsLeft(1000, 2_000_000)).toBe(0);
  });

  it("formats m:ss", () => {
    expect(formatCountdown(300)).toBe("5:00");
    expect(formatCountdown(299)).toBe("4:59");
    expect(formatCountdown(61)).toBe("1:01");
    expect(formatCountdown(9)).toBe("0:09");
    expect(formatCountdown(0)).toBe("0:00");
    expect(formatCountdown(-4)).toBe("0:00");
  });
});

describe("cancel kinds", () => {
  it("scopes a cancel to the dialog's own kind", () => {
    expect(cancelBody("agent")).toEqual({ kind: "agent" });
    expect(cancelBody("device")).toEqual({ kind: "device" });
  });

  it("the Devices panel never sends an unscoped cancel", () => {
    // An unscoped cancel drops EVERY pending code — closing the phone QR would kill an agent
    // code being typed on another machine. Every call site must pass a kind.
    expect(devicesSrc).not.toMatch(/pairingCancel\(\s*\)/);
    expect(devicesSrc).toMatch(/api\.pairingCancel\(kind\)/);
  });
});

describe("reachable addresses", () => {
  it("lists tailnet first, keeping order within a kind", () => {
    const hosts = [
      { host: "192.168.1.5", kind: "lan" },
      { host: "100.64.0.2", kind: "tailnet" },
      { host: "10.0.0.4", kind: "lan" },
    ];
    expect(tailnetFirst(hosts).map((h) => h.host)).toEqual(["100.64.0.2", "192.168.1.5", "10.0.0.4"]);
    expect(hostKindLabel("tailnet")).toBe("Tailnet");
    expect(hostKindLabel("lan")).toBe("LAN");
  });
});

describe("remote auth badge", () => {
  it("rejected → a warning that points at re-pair", () => {
    const b = remoteAuthBadge("rejected");
    expect(b?.status).toBe("warning");
    expect(b?.label).toBe("token rejected — re-pair");
    expect(b?.repair).toBe(true);
  });

  it("none → a neutral 'not paired'", () => {
    expect(remoteAuthBadge("none")).toMatchObject({ status: "neutral", label: "not paired" });
  });

  it("ok → a subtle success mark", () => {
    expect(remoteAuthBadge("ok")).toMatchObject({ status: "success", repair: false });
  });

  it("unknown / absent (older hub, first probe pending) → nothing", () => {
    expect(remoteAuthBadge("unknown")).toBeNull();
    expect(remoteAuthBadge(undefined)).toBeNull();
  });
});

describe("delegate URL selection", () => {
  const host: FleetAgent = { name: "main", id: "main", port: 7870, pid: 1, running: true, bundle: "", host: true, a2a: "http://127.0.0.1:7870/a2a" };
  const local: FleetAgent = { name: "ava", id: "ava-1a2b", port: 7890, pid: 2, running: true, bundle: "", a2a: "http://127.0.0.1:7890/a2a" };
  const remote: FleetAgent = {
    name: "remy", id: "remy-re01", port: 0, pid: null, running: true, bundle: "", remote: true,
    url: "http://100.64.0.9:7870", a2a: "http://127.0.0.1:7870/agents/remy-re01/a2a", auth: "ok",
  };
  const agents = [host, local, remote];

  it("from the hub's window, a remote target links through the hub's proxy (ADR 0113 D4)", () => {
    expect(delegateLinkFor(agents, "host", remote)).toEqual({ url: "http://127.0.0.1:7870/agents/remy-re01/a2a" });
  });

  it("from a local member's window, the hub-box URLs are valid (same box, fleet token)", () => {
    expect(delegateLinkFor(agents, "ava-1a2b", remote).url).toBe(remote.a2a);
    expect(delegateLinkFor(agents, "ava-1a2b", host).url).toBe(host.a2a);
  });

  it("from a REMOTE member's window, every hub-box URL is refused with a reason", () => {
    for (const target of [host, local]) {
      const link = delegateLinkFor(agents, "remy-re01", target);
      expect(link.url).toBeNull();
      expect(link.reason).toMatch(/remote agent on another machine/);
    }
    expect(hubUrlRefusal(agents, "remy-re01")).toMatch(/another machine/);
    // …and never falls back to the remote's raw URL, which carries no token.
    expect(JSON.stringify(delegateLinkFor(agents, "remy-re01", remote))).not.toContain("100.64.0.9");
  });

  it("an unknown focus is refused (a wrong URL in a config is worse than a disabled click)", () => {
    expect(delegateLinkFor(agents, "ghost", local).url).toBeNull();
    // The hub's own slug is always known, even before the roster lists it.
    expect(hubUrlRefusal([], "host")).toBeNull();
  });

  it("a member with no A2A endpoint can't be linked", () => {
    expect(delegateLinkFor(agents, "host", { ...remote, a2a: null }).url).toBeNull();
  });
});

describe("D10 plaintext opt-in (mirrors the hub's credential rule)", () => {
  it("https, loopback and tailnet addresses never ask", () => {
    for (const url of [
      "https://192.168.1.5:7870",
      "https://agent.example.com",
      "http://127.0.0.1:7870",
      "http://127.8.9.1:7870",
      "http://localhost:7870",
      "http://[::1]:7870",
      "http://100.64.0.1:7870",
      "http://100.100.100.100:7870",
      "http://100.127.255.255:7870",
      "http://ava.tail1234.ts.net:7870",
      "http://AVA.TAIL1234.TS.NET.:7870",
    ]) {
      expect([url, transportSecurity(url)]).toEqual([url, "secure"]);
      expect(needsInsecureOptIn(url)).toBe(false);
    }
  });

  it("plain http to any other literal address asks (LAN, public, and just outside 100.64/10)", () => {
    for (const url of [
      "http://192.168.1.5:7870",
      "http://10.0.0.4:7870",
      "http://172.16.3.2",
      "http://8.8.8.8:7870",
      "http://100.63.255.255:7870",
      "http://100.128.0.1:7870",
      "http://[fe80::1]:7870",
    ]) {
      expect([url, transportSecurity(url)]).toEqual([url, "insecure"]);
      expect(needsInsecureOptIn(url)).toBe(true);
    }
  });

  it("an http NAME is left to the hub (it resolves the name; a browser can't)", () => {
    expect(transportSecurity("http://studio.local:7870")).toBe("unknown");
    expect(needsInsecureOptIn("http://studio.local:7870")).toBe(false);
    expect(transportSecurity("http://ts.net.evil.com:7870")).toBe("unknown");
  });

  it("garbage isn't classified", () => {
    expect(transportSecurity("")).toBe("invalid");
    expect(transportSecurity("ftp://192.168.1.5")).toBe("invalid");
    expect(transportSecurity("192.168.1.5:7870")).toBe("invalid");
  });

  it("recognizes the hub's refusal so the opt-in can be revealed after a 400", () => {
    const refusal = Object.assign(new Error("refusing to send a credential over plain http to 192.168.1.9 — pass allow_insecure"), { status: 400 });
    expect(isInsecureRefusal(refusal)).toBe(true);
    const expired = Object.assign(new Error("that code is invalid or expired — generate a new one on the remote"), { status: 400 });
    expect(isInsecureRefusal(expired)).toBe(false);
    const unreachable = Object.assign(new Error("http://x is unreachable (ConnectError)"), { status: 502 });
    expect(isInsecureRefusal(unreachable)).toBe(false);
  });
});
