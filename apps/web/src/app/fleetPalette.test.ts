import { describe, it, expect, beforeEach } from "vitest";

import { fleetPaletteEntries, markAgentOpened, readAgentRecency, togglableFleetAgents } from "./fleetPalette";
import type { FleetAgent } from "../lib/types";

function agent(over: Partial<FleetAgent>): FleetAgent {
  return { name: "a", id: "a", port: 7870, pid: 1, running: true, bundle: "", ...over };
}

describe("fleetPaletteEntries (#1733)", () => {
  it("lists other agents reachable-first then alphabetical, omitting the focused one", () => {
    const agents = [
      agent({ name: "zed", id: "zed", running: true }),
      agent({ name: "ava", id: "ava", running: true }),
      agent({ name: "down", id: "down", running: false }),
      agent({ name: "me", id: "me", running: true }),
    ];
    const out = fleetPaletteEntries(agents, "me");
    expect(out.map((e) => e.slug)).toEqual(["ava", "zed", "down"]); // reachable alpha, then the down one
    expect(out.find((e) => e.slug === "me")).toBeUndefined(); // the focused agent is omitted
    expect(out.find((e) => e.slug === "down")!.disabled).toBe(true);
  });

  it("routes the host entry by the literal 'host' slug, not its id", () => {
    const out = fleetPaletteEntries([agent({ name: "Main", id: "ignored", host: true })], "other");
    expect(out[0].slug).toBe("host");
  });

  it("floats a recently-opened agent above alphabetical order", () => {
    const agents = [agent({ name: "ava", id: "ava" }), agent({ name: "bob", id: "bob" })];
    expect(fleetPaletteEntries(agents, "me", { bob: 999 }).map((e) => e.slug)).toEqual(["bob", "ava"]);
  });

  it("shows a down remote as disabled with an 'unreachable' hint", () => {
    const [only] = fleetPaletteEntries([agent({ name: "r", id: "r", remote: true, running: false })], "me");
    expect(only.disabled).toBe(true);
    expect(only.hint).toBe("unreachable");
  });

  it("labels a reachable remote 'remote · switch'", () => {
    const [only] = fleetPaletteEntries([agent({ name: "r", id: "r", remote: true, running: true })], "me");
    expect(only.disabled).toBe(false);
    expect(only.hint).toBe("remote · switch");
  });
});

describe("togglableFleetAgents (#1769)", () => {
  it("excludes the host — it serves this console and can't be stopped from itself", () => {
    const agents = [
      agent({ name: "main", id: "main", host: true }),
      agent({ name: "ava", id: "ava" }),
    ];
    const out = togglableFleetAgents(agents);
    expect(out.map((a) => a.name)).toEqual(["ava"]);
    expect(out.find((a) => a.host)).toBeUndefined();
  });

  it("excludes remotes — they have no local process to start/stop from here", () => {
    const agents = [
      agent({ name: "ava", id: "ava" }),
      agent({ name: "remy", id: "remy", remote: true, running: true }),
    ];
    expect(togglableFleetAgents(agents).map((a) => a.name)).toEqual(["ava"]);
  });

  it("lists both running and stopped local members (on/off is live process state)", () => {
    const agents = [
      agent({ name: "up", id: "up", running: true }),
      agent({ name: "down", id: "down", running: false, pid: null }),
    ];
    const out = togglableFleetAgents(agents);
    expect(out.map((a) => a.name)).toEqual(["down", "up"]); // both present, name-sorted
    expect(out.map((a) => a.running)).toEqual([false, true]);
  });

  it("sorts stably by display name", () => {
    const agents = [
      agent({ name: "zed", id: "zed" }),
      agent({ name: "ava", id: "ava" }),
      agent({ name: "bob", id: "bob" }),
    ];
    expect(togglableFleetAgents(agents).map((a) => a.name)).toEqual(["ava", "bob", "zed"]);
  });

  it("does not mutate the input array", () => {
    const agents = [agent({ name: "zed", id: "zed" }), agent({ name: "ava", id: "ava" })];
    const snapshot = agents.map((a) => a.name);
    togglableFleetAgents(agents);
    expect(agents.map((a) => a.name)).toEqual(snapshot);
  });
});

describe("agent recency store", () => {
  beforeEach(() => localStorage.clear());

  it("records and reads back last-opened timestamps", () => {
    markAgentOpened("ava", 100);
    markAgentOpened("bob", 200);
    expect(readAgentRecency()).toEqual({ ava: 100, bob: 200 });
  });

  it("returns an empty map when nothing is stored", () => {
    expect(readAgentRecency()).toEqual({});
  });
});
