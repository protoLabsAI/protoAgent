import { describe, it, expect } from "vitest";

import {
  applyRosterOrder,
  BOX_RUNTIME_KEYS,
  canAddRemote,
  canMoveDown,
  canMoveUp,
  fleetAutostartRoster,
  memberAutostarts,
  moveDisabled,
  moveLabel,
  reorderRoster,
  rosterOrder,
  slugOf,
  updateAutostartRoster,
} from "./FleetManagerPanel";
import type { FleetAgent } from "../lib/types";

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

// A minimal fleet fixture — the host, a local member, and a remote — keyed by IMMUTABLE id
// with editable display names/labels that must NEVER be what reordering keys on (#3197).
const agent = (over: Partial<FleetAgent> & Pick<FleetAgent, "id" | "name">): FleetAgent => ({
  port: 7870,
  pid: null,
  running: false,
  bundle: "",
  ...over,
});
const FLEET: FleetAgent[] = [
  agent({ id: "host-1", name: "host", host: true }),
  agent({ id: "agent-17", name: "engineer", label: "Engineer" }),
  agent({ id: "remote-9", name: "ava", remote: true }),
];

describe("roster reorder — the immutable-id payload the merged hub API takes (#3197)", () => {
  it("rosterOrder is the complete member order, by id, never the editable name/label", () => {
    expect(rosterOrder(FLEET)).toEqual(["host-1", "agent-17", "remote-9"]);
  });

  it("moving a row DOWN submits the full permutation with that pair swapped", () => {
    // Move the host (index 0) down past the local member.
    expect(reorderRoster(rosterOrder(FLEET), 0, 1)).toEqual(["agent-17", "host-1", "remote-9"]);
  });

  it("moving a row UP submits the full permutation with that pair swapped", () => {
    // Move the remote (index 2) up past the local member.
    expect(reorderRoster(rosterOrder(FLEET), 2, -1)).toEqual(["host-1", "remote-9", "agent-17"]);
  });

  it("keeps every id exactly once — a move never drops or duplicates a member", () => {
    const next = reorderRoster(rosterOrder(FLEET), 1, 1);
    expect([...next].sort()).toEqual([...rosterOrder(FLEET)].sort());
    expect(new Set(next).size).toBe(next.length);
  });
});

describe("roster reorder — boundary + busy disabled state (#3197)", () => {
  const count = FLEET.length;

  it("the first row can't move up; the last can't move down", () => {
    expect(canMoveUp(0)).toBe(false);
    expect(canMoveUp(1)).toBe(true);
    expect(canMoveDown(count - 1, count)).toBe(false);
    expect(canMoveDown(0, count)).toBe(true);
  });

  it("a move that would fall off a boundary is a no-op — the id list is unchanged", () => {
    const ids = rosterOrder(FLEET);
    expect(reorderRoster(ids, 0, -1)).toBe(ids); // top row, up
    expect(reorderRoster(ids, count - 1, 1)).toBe(ids); // bottom row, down
  });

  it("moveDisabled gates on the boundary when idle", () => {
    expect(moveDisabled(-1, 0, count, false)).toBe(true); // first row, up
    expect(moveDisabled(1, count - 1, count, false)).toBe(true); // last row, down
    expect(moveDisabled(-1, 1, count, false)).toBe(false); // a middle row, up
    expect(moveDisabled(1, 1, count, false)).toBe(false); // a middle row, down
  });

  it("moveDisabled disables EVERY control while a reorder is in flight (busy)", () => {
    expect(moveDisabled(-1, 1, count, true)).toBe(true);
    expect(moveDisabled(1, 1, count, true)).toBe(true);
    expect(moveDisabled(1, 0, count, true)).toBe(true);
  });
});

describe("roster reorder — reconciliation never loses a row or a row action (#3197)", () => {
  it("optimistically applies the submitted order to the live agents", () => {
    const next = applyRosterOrder(FLEET, ["agent-17", "remote-9", "host-1"]);
    expect(next.map((a) => a.id)).toEqual(["agent-17", "remote-9", "host-1"]);
    // The SAME agent objects ride through — name, label, url, process state, host/remote
    // flags, and id are all untouched (reorder is presentation-only).
    expect(next.find((a) => a.id === "host-1")).toBe(FLEET[0]);
    expect(next.find((a) => a.id === "remote-9")?.remote).toBe(true);
  });

  it("a stale order (missing a just-added member) still keeps every current row", () => {
    // Server reconciliation the client mirrors: an id not named in the order keeps discovery
    // order AFTER every ranked id — the new member is never dropped.
    const withNew = [...FLEET, agent({ id: "agent-42", name: "fresh" })];
    const next = applyRosterOrder(withNew, ["remote-9", "host-1", "agent-17"]);
    expect(next.map((a) => a.id)).toEqual(["remote-9", "host-1", "agent-17", "agent-42"]);
    expect(next).toHaveLength(withNew.length);
  });

  it("an order naming an unknown/removed id doesn't corrupt the row set", () => {
    const next = applyRosterOrder(FLEET, ["gone", "remote-9", "host-1", "agent-17"]);
    expect([...next.map((a) => a.id)].sort()).toEqual(["agent-17", "host-1", "remote-9"]);
    expect(next).toHaveLength(FLEET.length);
  });

  it("an empty order is identity (the default host/local/remote order)", () => {
    expect(applyRosterOrder(FLEET, [])).toBe(FLEET);
  });
});

describe("roster reorder — accessible control names (#3197)", () => {
  it("names each control by direction and the member's DISPLAY name (label ?? name)", () => {
    expect(moveLabel(-1, { name: "engineer", label: "Engineer" })).toBe("Move Engineer up");
    expect(moveLabel(1, { name: "engineer", label: "Engineer" })).toBe("Move Engineer down");
  });

  it("falls back to name when a row has no verbatim label", () => {
    expect(moveLabel(-1, { name: "ava" })).toBe("Move ava up");
    expect(moveLabel(1, { name: "ava" })).toBe("Move ava down");
  });
});
