import { describe, it, expect } from "vitest";

import {
  BOX_RUNTIME_KEYS,
  canAddRemote,
  canMove,
  fleetAutostartRoster,
  fleetOrderIds,
  memberAutostarts,
  moveDisabled,
  moveInList,
  moveLabel,
  orderAgentsByIds,
  reorderByDrag,
  sameOrder,
  shouldSubmitOrder,
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

// ── Manual roster reordering (#3197) — the pure ordering core ───────────────────────────
// The API payload, the drag / move math, boundary + busy disabling, accessible labels and the
// failure-safe reconciliation are all pure, so they're unit-tested without rendering the panel
// (the console has no @testing-library; this suite is `.test.ts` only).

describe("fleetOrderIds — the complete immutable-id order PUT to /api/fleet/order", () => {
  it("is the roster ids in display order — stable ids, never the editable name/label", () => {
    const agents = [
      { id: "host-1", name: "main", host: true },
      { id: "ava-01", name: "ava", label: "Ava 🚀" },
      { id: "roxy-9", name: "roxy" },
    ];
    // ids, not names: a rename can't perturb the persisted order.
    expect(fleetOrderIds(agents)).toEqual(["host-1", "ava-01", "roxy-9"]);
  });
});

describe("moveInList — the move-up / move-down control payload", () => {
  const ids = ["host", "ava", "roxy"];

  it("moves a row up one slot", () => {
    expect(moveInList(ids, 2, "up")).toEqual(["host", "roxy", "ava"]);
  });

  it("moves a row down one slot", () => {
    expect(moveInList(ids, 1, "down")).toEqual(["host", "roxy", "ava"]);
  });

  it("returns the SAME order (identity) at a boundary, so the caller skips a no-op PUT", () => {
    expect(moveInList(ids, 0, "up")).toBe(ids);
    expect(moveInList(ids, 2, "down")).toBe(ids);
    expect(moveInList(ids, -1, "up")).toBe(ids); // unknown row (indexOf miss)
  });

  it("does not mutate the input array", () => {
    const input = ["a", "b", "c"];
    moveInList(input, 0, "down");
    expect(input).toEqual(["a", "b", "c"]);
  });
});

describe("reorderByDrag — the drag-and-drop payload", () => {
  const ids = ["host", "ava", "roxy", "remy"];

  it("pulls the dragged id out and re-inserts it at the target slot (drag down)", () => {
    expect(reorderByDrag(ids, 1, 3)).toEqual(["host", "roxy", "remy", "ava"]);
  });

  it("re-inserts above the target when dragging up", () => {
    expect(reorderByDrag(ids, 3, 0)).toEqual(["remy", "host", "ava", "roxy"]);
  });

  it("lets a member drop ABOVE the pinned host (host has no handle, but is a drop slot)", () => {
    expect(reorderByDrag(ids, 2, 0)).toEqual(["roxy", "host", "ava", "remy"]);
  });

  it("returns the SAME order for a self-drop or an out-of-range index (no-op PUT)", () => {
    expect(reorderByDrag(ids, 2, 2)).toBe(ids);
    expect(reorderByDrag(ids, 2, -1)).toBe(ids);
    expect(reorderByDrag(ids, 9, 0)).toBe(ids);
  });
});

describe("canMove / moveDisabled — boundary + busy disabling of the move controls", () => {
  it("disables move-up only at the top and move-down only at the bottom", () => {
    expect(canMove(0, "up", 3)).toBe(false);
    expect(canMove(1, "up", 3)).toBe(true);
    expect(canMove(2, "down", 3)).toBe(false);
    expect(canMove(1, "down", 3)).toBe(true);
  });

  it("a member just below the pinned host can still move up (full-array indices)", () => {
    // host at 0, first member at 1 → move-up is enabled and swaps it above the host.
    expect(moveDisabled(1, "up", 3, false)).toBe(false);
  });

  it("disables every move control while a reorder save is pending (busy), even off a boundary", () => {
    expect(moveDisabled(1, "up", 3, true)).toBe(true);
    expect(moveDisabled(1, "down", 3, true)).toBe(true);
    // …and stays disabled at a boundary regardless of pending.
    expect(moveDisabled(0, "up", 3, false)).toBe(true);
  });
});

describe("moveLabel — the move control's accessible name", () => {
  it("names the DISPLAY name (label ?? name), never the id", () => {
    expect(moveLabel({ name: "ava", label: "Ava 🚀" }, "up")).toBe("Move Ava 🚀 up");
    expect(moveLabel({ name: "roxy" }, "down")).toBe("Move roxy down");
  });
});

describe("sameOrder — skip a no-op PUT", () => {
  it("is true only for an element-wise identical order", () => {
    expect(sameOrder(["a", "b"], ["a", "b"])).toBe(true);
    expect(sameOrder(["a", "b"], ["b", "a"])).toBe(false);
    expect(sameOrder(["a"], ["a", "b"])).toBe(false);
  });
});

describe("shouldSubmitOrder — the single guard shared by the drag AND move-control paths", () => {
  const current = ["host", "ava", "roxy"];

  it("submits a real change only when no reorder save is in flight", () => {
    expect(shouldSubmitOrder(["ava", "host", "roxy"], current, false)).toBe(true);
  });

  it("blocks a SECOND concurrent full-order PUT while a save is pending (the drag-path fix)", () => {
    // Even a genuinely different order is refused mid-save: a concurrent PUT could complete out of
    // order, or an earlier failure's roll-back could clobber this order. The drag handle disables
    // for the same reason the move buttons already did (moveDisabled(..., pending)).
    expect(shouldSubmitOrder(["ava", "host", "roxy"], current, true)).toBe(false);
  });

  it("skips a no-op PUT (self-drop / boundary move) regardless of pending", () => {
    expect(shouldSubmitOrder(["host", "ava", "roxy"], current, false)).toBe(false);
    expect(shouldSubmitOrder(["host", "ava", "roxy"], current, true)).toBe(false);
  });
});

describe("orderAgentsByIds — failure-safe reconciliation (no row is ever lost)", () => {
  const agents = [
    { id: "host", name: "main" },
    { id: "ava", name: "ava" },
    { id: "roxy", name: "roxy" },
  ];

  it("ranks the agents to match the order (the optimistic cache write)", () => {
    expect(orderAgentsByIds(agents, ["roxy", "host", "ava"]).map((a) => a.id)).toEqual([
      "roxy",
      "host",
      "ava",
    ]);
  });

  it("keeps a live agent the order OMITS — appended at the tail, never dropped", () => {
    // A member registered between the optimistic write and the server echo isn't in `order`.
    expect(orderAgentsByIds(agents, ["ava", "host"]).map((a) => a.id)).toEqual([
      "ava",
      "host",
      "roxy",
    ]);
  });

  it("ignores an id that is no longer a live row (a member removed under us)", () => {
    expect(orderAgentsByIds(agents, ["gone", "roxy", "ava", "host"]).map((a) => a.id)).toEqual([
      "roxy",
      "ava",
      "host",
    ]);
  });

  it("preserves every row object (existing row actions survive a success or a roll-back)", () => {
    const ranked = orderAgentsByIds(agents, ["roxy", "ava", "host"]);
    expect(ranked).toHaveLength(agents.length);
    expect(new Set(ranked)).toEqual(new Set(agents)); // same objects, just reordered
  });
});
