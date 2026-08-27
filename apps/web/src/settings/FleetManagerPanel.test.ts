import { describe, it, expect } from "vitest";

import type { FleetAgent } from "../lib/types";
import {
  BOX_RUNTIME_KEYS,
  applyFleetOrder,
  canAddRemote,
  fleetAutostartRoster,
  memberAutostarts,
  moveLabel,
  reorderFleetIds,
  slugOf,
  updateAutostartRoster,
} from "./FleetManagerPanel";

describe("canAddRemote — manual add-remote submit gate (ADR 0042 §I)", () => {
  it("requires a non-empty name", () => {
    expect(canAddRemote("", "http://100.64.0.9:7870")).toBe(false);
    expect(canAddRemote("   ", "http://100.64.0.9:7870")).toBe(false);
    expect(canAddRemote("ava", "http://100.64.0.9:7870")).toBe(true);
  });

  it("requires an http(s) URL", () => {
    expect(canAddRemote("ava", "")).toBe(false);
    expect(canAddRemote("ava", "100.64.0.9:7870")).toBe(false); // no scheme
    expect(canAddRemote("ava", "ftp://host")).toBe(false);
    expect(canAddRemote("ava", "ws://host")).toBe(false); // ws:// isn't the register URL (it's http)
    expect(canAddRemote("ava", "https://ava.example:7870")).toBe(true);
  });

  it("trims before validating", () => {
    expect(canAddRemote("  ava  ", "  http://host:7870  ")).toBe(true);
  });
});

describe("slugOf — the row link's destination (#2240)", () => {
  it("maps this instance to the reserved host slug", () => {
    expect(slugOf({ id: "abc123", host: true })).toBe("host");
  });

  it("uses the stable id for every other member", () => {
    expect(slugOf({ id: "abc123" })).toBe("abc123");
    expect(slugOf({ id: "abc123", host: false })).toBe("abc123");
  });

  it("never uses the display name — a rename must not move the agent's URL", () => {
    // The panel renames in place (id survives); the link has to keep pointing at the id.
    expect(slugOf({ id: "abc123" } as { id: string; name?: string })).toBe("abc123");
  });
});

describe("BOX_RUNTIME_KEYS — the Box runtime chip's field set (#2880)", () => {
  it("keeps the keep-warm fields but leaves per-member autostart on each row", () => {
    expect(BOX_RUNTIME_KEYS).not.toContain("fleet.autostart");
    expect(BOX_RUNTIME_KEYS).toContain("fleet.warm.max");
    expect(BOX_RUNTIME_KEYS).toContain("fleet.warm.grace_seconds");
  });

  it("pins the full set — a dropped key would silently vanish from the popover", () => {
    // QuickSetting resolves keys against the schema and silently skips misses, so a
    // typo or accidental removal here shows up nowhere else. Golden-pin the list.
    expect(BOX_RUNTIME_KEYS).toEqual([
      "network.bind",
      "fleet.port_base",
      "fleet.discovery.port_min",
      "fleet.discovery.port_max",
      "fleet.discovery.mdns",
      "fleet.warm.max",
      "fleet.warm.grace_seconds",
    ]);
  });
});

describe("fleet member autostart rows", () => {
  const member = { id: "agent-17", name: "protoEngineer" };

  it("reads fleet.autostart from the real settings-schema shape", () => {
    const groups = [{ id: "fleet", label: "Fleet", fields: [
      { key: "fleet.autostart", value: ["agent-17", "reviewer"] },
    ] }];
    expect(fleetAutostartRoster(groups as never)).toEqual(["agent-17", "reviewer"]);
  });

  it("recognizes both stable ids and legacy name entries", () => {
    expect(memberAutostarts(["agent-17"], member)).toBe(true);
    expect(memberAutostarts(["protoEngineer"], member)).toBe(true);
    expect(memberAutostarts(["reviewer"], member)).toBe(false);
  });

  it("writes stable ids and preserves unrelated or currently missing members", () => {
    expect(updateAutostartRoster(["reviewer"], member, true)).toEqual(["reviewer", "agent-17"]);
    expect(updateAutostartRoster(["reviewer", "protoEngineer"], member, false)).toEqual(["reviewer"]);
  });
});

// A minimal FleetAgent for the reorder tests — only the fields the reorder path touches matter,
// but the full shape is filled so a row carried through applyFleetOrder is byte-for-byte intact.
const agent = (id: string, extra: Partial<FleetAgent> = {}): FleetAgent => ({
  name: id,
  id,
  port: 7870,
  pid: null,
  running: false,
  bundle: "",
  ...extra,
});

describe("reorderFleetIds — the move-up/move-down submit payload (#3197)", () => {
  const agents = [agent("host", { host: true }), agent("local-a"), agent("remote-b", { remote: true })];

  it("swaps a member with its neighbor and returns the COMPLETE id permutation", () => {
    // Move local-a up: it trades places with host, and every id (host + local + remote) is still present.
    expect(reorderFleetIds(agents, 1, -1)).toEqual(["local-a", "host", "remote-b"]);
    // Move local-a down: it trades places with remote-b.
    expect(reorderFleetIds(agents, 1, 1)).toEqual(["host", "remote-b", "local-a"]);
  });

  it("submits IMMUTABLE ids, never editable display names", () => {
    const renamed = [agent("id-1", { name: "renamed-away" }), agent("id-2", { name: "also-renamed" })];
    expect(reorderFleetIds(renamed, 0, 1)).toEqual(["id-2", "id-1"]);
  });

  it("is a no-op at the boundaries — first can't move up, last can't move down", () => {
    expect(reorderFleetIds(agents, 0, -1)).toEqual(["host", "local-a", "remote-b"]);
    expect(reorderFleetIds(agents, agents.length - 1, 1)).toEqual(["host", "local-a", "remote-b"]);
    // Out-of-range indices degrade to the unchanged order rather than throwing.
    expect(reorderFleetIds(agents, 9, -1)).toEqual(["host", "local-a", "remote-b"]);
  });

  it("always emits every id even from a single-move — a partial slice would be rejected", () => {
    expect(reorderFleetIds(agents, 2, -1)).toHaveLength(agents.length);
  });
});

describe("applyFleetOrder — optimistic + reconcile snapshot transform (#3197)", () => {
  const host = agent("host", { host: true });
  const localA = agent("local-a");
  const remoteB = agent("remote-b", { remote: true, url: "http://100.64.0.9:7870" });
  const agents = [host, localA, remoteB];

  it("reorders rows to the submitted id order, carrying every row over by reference", () => {
    const next = applyFleetOrder(agents, ["local-a", "remote-b", "host"]);
    expect(next.map((a) => a.id)).toEqual(["local-a", "remote-b", "host"]);
    // Same objects — names, urls, tokens, process state, ids and per-row actions are untouched.
    expect(next[0]).toBe(localA);
    expect(next[1]).toBe(remoteB);
    expect(next[2]).toBe(host);
  });

  it("retains ALL current rows after a reorder — none dropped, none duplicated", () => {
    const next = applyFleetOrder(agents, ["remote-b", "local-a", "host"]);
    expect(next).toHaveLength(agents.length);
    expect(new Set(next.map((a) => a.id))).toEqual(new Set(["host", "local-a", "remote-b"]));
  });

  it("keeps a row added between submit and reconcile — an id absent from `order` stays (tail)", () => {
    // The server persists the submitted order and the 3s poll reconciles; until then a freshly
    // added member (not in the optimistic `order`) must still render, not vanish.
    const withNew = [...agents, agent("late-c")];
    const next = applyFleetOrder(withNew, ["local-a", "host", "remote-b"]);
    expect(next.map((a) => a.id)).toEqual(["local-a", "host", "remote-b", "late-c"]);
  });

  it("ignores unknown ids in `order` (a just-removed member) without erroring", () => {
    const next = applyFleetOrder(agents, ["ghost", "host", "local-a", "remote-b"]);
    expect(next.map((a) => a.id)).toEqual(["host", "local-a", "remote-b"]);
  });

  it("does NOT mutate the input snapshot — so an error rollback restores it exactly", () => {
    const before = agents.slice();
    applyFleetOrder(agents, ["remote-b", "host", "local-a"]);
    expect(agents).toEqual(before);
    expect(agents.map((a) => a.id)).toEqual(["host", "local-a", "remote-b"]);
  });
});

describe("moveLabel — accessible name for the icon-only reorder controls (#3197)", () => {
  it("names the direction and the member so a chevron button isn't unlabeled", () => {
    expect(moveLabel("protoEngineer", -1)).toBe("Move protoEngineer up in the fleet order");
    expect(moveLabel("protoEngineer", 1)).toBe("Move protoEngineer down in the fleet order");
  });

  it("uses the display label passed in (label ?? name), never the id", () => {
    expect(moveLabel("Ava — reviewer", -1)).toBe("Move Ava — reviewer up in the fleet order");
  });
});
