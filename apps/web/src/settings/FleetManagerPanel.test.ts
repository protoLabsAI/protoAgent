import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import type { FleetAgent } from "../lib/types";
import {
  BOX_RUNTIME_KEYS,
  FleetManagerPanel,
  canAddRemote,
  canMove,
  fleetAutostartRoster,
  fleetOrderIds,
  memberAutostarts,
  moveDisabled,
  moveInList,
  moveLabel,
  moveMember,
  orderAgentsByIds,
  reorderByDrag,
  sameOrder,
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

// ── Manual roster reordering (#3197) — pure ordering math ─────────────────────
const ROSTER: FleetAgent[] = [
  { id: "host-0", name: "host", host: true, port: 7870, pid: 100, running: true, bundle: "", version: "1.0.0" },
  { id: "m1", name: "alpha", label: "Alpha ⚡", port: 7871, pid: 200, running: true, bundle: "" },
  { id: "m2", name: "bravo", port: 7872, pid: null, running: false, bundle: "" },
];

describe("fleetOrderIds — the complete immutable-id payload (#3197)", () => {
  it("returns every id in display order — the complete permutation the API requires", () => {
    expect(fleetOrderIds(ROSTER)).toEqual(["host-0", "m1", "m2"]);
  });

  it("carries stable ids, NEVER editable names/labels (a rename must not perturb order)", () => {
    const renamed = ROSTER.map((a) => ({ ...a, name: `${a.name}-renamed`, label: "changed" }));
    expect(fleetOrderIds(renamed)).toEqual(["host-0", "m1", "m2"]);
  });
});

describe("moveInList — immutable single-item move", () => {
  it("moves an item and returns a new array (leaves the input untouched)", () => {
    const input = ["a", "b", "c"];
    expect(moveInList(input, 0, 2)).toEqual(["b", "c", "a"]);
    expect(input).toEqual(["a", "b", "c"]);
  });

  it("is a no-op for an equal or out-of-range index (can't drop, dupe, or lose a row)", () => {
    const input = ["a", "b", "c"];
    expect(moveInList(input, 1, 1)).toBe(input);
    expect(moveInList(input, -1, 0)).toBe(input);
    expect(moveInList(input, 0, 3)).toBe(input);
  });
});

describe("reorderByDrag — the pointer path (#3197)", () => {
  it("lands the dragged member at the drop target's slot (drag down)", () => {
    expect(fleetOrderIds(reorderByDrag(ROSTER, "host-0", "m2"))).toEqual(["m1", "m2", "host-0"]);
  });

  it("lands the dragged member at the drop target's slot (drag up)", () => {
    expect(fleetOrderIds(reorderByDrag(ROSTER, "m2", "host-0"))).toEqual(["m2", "host-0", "m1"]);
  });

  it("is a no-op for a self-drop or an unknown id", () => {
    expect(reorderByDrag(ROSTER, "m1", "m1")).toBe(ROSTER);
    expect(reorderByDrag(ROSTER, "ghost", "m1")).toBe(ROSTER);
    expect(reorderByDrag(ROSTER, "m1", "ghost")).toBe(ROSTER);
  });
});

describe("moveMember — the accessible non-pointer path, equivalent to a drag (#3197)", () => {
  it("shifts a member one slot up/down", () => {
    expect(fleetOrderIds(moveMember(ROSTER, "m1", "up"))).toEqual(["m1", "host-0", "m2"]);
    expect(fleetOrderIds(moveMember(ROSTER, "m1", "down"))).toEqual(["host-0", "m2", "m1"]);
  });

  it("is a no-op at the list boundary (top can't go up, bottom can't go down)", () => {
    expect(moveMember(ROSTER, "host-0", "up")).toBe(ROSTER); // already first
    expect(moveMember(ROSTER, "m2", "down")).toBe(ROSTER); // already last
  });

  it("produces the SAME order a drag between the two rows would (control ≡ drag)", () => {
    expect(fleetOrderIds(moveMember(ROSTER, "m1", "up"))).toEqual(
      fleetOrderIds(reorderByDrag(ROSTER, "m1", "host-0")),
    );
  });
});

describe("canMove / moveDisabled — boundary + busy gating (#3197)", () => {
  it("disables up at the top and down at the bottom", () => {
    expect(canMove(0, 3, "up")).toBe(false);
    expect(canMove(0, 3, "down")).toBe(true);
    expect(canMove(2, 3, "up")).toBe(true);
    expect(canMove(2, 3, "down")).toBe(false);
  });

  it("disables every control while a save is pending, even mid-list", () => {
    expect(moveDisabled(1, 3, "up", false)).toBe(false); // mid-list, idle → enabled
    expect(moveDisabled(1, 3, "up", true)).toBe(true); // …but pending disables it
    expect(moveDisabled(1, 3, "down", true)).toBe(true);
    expect(moveDisabled(0, 3, "up", false)).toBe(true); // boundary disables regardless
  });
});

describe("moveLabel — the icon-only control's accessible name (#3197)", () => {
  it("names the member and the direction", () => {
    expect(moveLabel("up", { name: "bravo" })).toBe("Move bravo up");
    expect(moveLabel("down", { name: "bravo" })).toBe("Move bravo down");
  });

  it("prefers the display label over the raw name, and never the id", () => {
    expect(moveLabel("up", { name: "alpha", label: "Alpha ⚡" })).toBe("Move Alpha ⚡ up");
  });
});

describe("orderAgentsByIds — optimistic write + reconciliation, never loses a row (#3197)", () => {
  it("reorders while preserving every row object (only the sequence changes)", () => {
    const out = orderAgentsByIds(ROSTER, ["m2", "host-0", "m1"]);
    expect(out.map((a) => a.id)).toEqual(["m2", "host-0", "m1"]);
    expect(out[0]).toBe(ROSTER[2]); // same object, not a copy
  });

  it("keeps a roster member missing from the order, appended in its original spot (no lost row)", () => {
    // A poll landing mid-reorder can hand back a member the submitted order didn't include.
    const out = orderAgentsByIds(ROSTER, ["m2", "host-0"]);
    expect(out.map((a) => a.id)).toEqual(["m2", "host-0", "m1"]);
  });

  it("drops an id that is no longer a roster member (removed while dragging)", () => {
    const out = orderAgentsByIds(ROSTER, ["gone", "m1", "host-0", "m2"]);
    expect(out.map((a) => a.id)).toEqual(["m1", "host-0", "m2"]);
  });

  it("round-trips a rollback: re-applying the previous ids restores the original order", () => {
    const previousIds = fleetOrderIds(ROSTER);
    const optimistic = orderAgentsByIds(ROSTER, ["m2", "m1", "host-0"]);
    expect(fleetOrderIds(orderAgentsByIds(optimistic, previousIds))).toEqual(previousIds);
  });
});

describe("sameOrder — the no-op guard", () => {
  it("is true only for identical sequences", () => {
    expect(sameOrder(["a", "b"], ["a", "b"])).toBe(true);
    expect(sameOrder(["a", "b"], ["b", "a"])).toBe(false);
    expect(sameOrder(["a", "b"], ["a"])).toBe(false);
  });
});

// ── Manual roster reordering (#3197) — the wired panel ────────────────────────
// jsdom + react-dom/client (the console has no @testing-library; the unit harness is
// `.test.ts` only, so elements are built with React.createElement, not JSX). Same pattern as
// settings/ProvidersPanel.ui.test.ts — a QueryClientProvider + ToastProvider around the panel
// with the mount-time api reads mocked.
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const AGENTS: FleetAgent[] = [
  { id: "host-0", name: "host", host: true, port: 7870, pid: 100, running: true, bundle: "", version: "1.0.0" },
  { id: "m1", name: "alpha", port: 7871, pid: 200, running: true, bundle: "" },
  { id: "m2", name: "bravo", port: 7872, pid: null, running: false, bundle: "" },
];

let container: HTMLElement;
let root: Root;

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function mockMountReads(agents: FleetAgent[] = AGENTS) {
  vi.spyOn(api, "fleet").mockResolvedValue({ agents });
  vi.spyOn(api, "settingsSchema").mockResolvedValue({ groups: [] } as never);
  vi.spyOn(api, "delegates").mockResolvedValue({ delegates: [] } as never);
}

async function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  act(() => {
    root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(FleetManagerPanel))));
  });
  await flush();
  await flush();
}

function moveButton(label: string): HTMLButtonElement | undefined {
  return [...document.body.querySelectorAll("button")].find(
    (b) => b.getAttribute("aria-label") === label,
  ) as HTMLButtonElement | undefined;
}

function moveButtons(): HTMLButtonElement[] {
  return [...document.body.querySelectorAll("button")].filter((b) =>
    /^Move /.test(b.getAttribute("aria-label") || ""),
  ) as HTMLButtonElement[];
}

function orderedUpLabels(): (string | null)[] {
  return [...document.body.querySelectorAll("button")]
    .map((b) => b.getAttribute("aria-label"))
    .filter((label): label is string => Boolean(label) && / up$/.test(label!));
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("FleetManagerPanel reorder UI (#3197)", () => {
  it("gives each eligible row accessible move controls and disables them at the list boundaries", async () => {
    mockMountReads();
    vi.spyOn(api, "reorderFleet").mockResolvedValue({ ok: true, order: [] });
    await renderPanel();

    // Every member row (host + local) gets an up + down control → 3 rows × 2.
    expect(moveButtons()).toHaveLength(6);
    // Accessible names name the member and direction.
    expect(moveButton("Move host up")).toBeDefined();
    expect(moveButton("Move bravo down")).toBeDefined();
    // Boundaries: the top row can't go up, the bottom row can't go down; the middle is free.
    expect(moveButton("Move host up")!.disabled).toBe(true);
    expect(moveButton("Move host down")!.disabled).toBe(false);
    expect(moveButton("Move alpha up")!.disabled).toBe(false);
    expect(moveButton("Move alpha down")!.disabled).toBe(false);
    expect(moveButton("Move bravo up")!.disabled).toBe(false);
    expect(moveButton("Move bravo down")!.disabled).toBe(true);
  });

  it("submits the COMPLETE immutable-id order when a move control is activated", async () => {
    mockMountReads();
    const reorderFleet = vi.spyOn(api, "reorderFleet").mockResolvedValue({ ok: true, order: [] });
    await renderPanel();

    await act(async () => {
      moveButton("Move alpha up")!.click();
      await Promise.resolve();
    });
    await flush();

    expect(reorderFleet).toHaveBeenCalledTimes(1);
    expect(reorderFleet).toHaveBeenCalledWith(["m1", "host-0", "m2"]);
  });

  it("submits the same complete order via drag-and-drop (the pointer path)", async () => {
    mockMountReads();
    const reorderFleet = vi.spyOn(api, "reorderFleet").mockResolvedValue({ ok: true, order: [] });
    await renderPanel();

    const rows = [...(container.querySelector(".fleet-list") as HTMLElement).children] as HTMLElement[];
    const alphaHandle = rows[1].querySelector(".fleet-drag-handle") as HTMLElement;
    const hostRow = rows[0];

    // Drag alpha (the dragged id is held in state, not dataTransfer) and drop it on the host row.
    act(() => {
      alphaHandle.dispatchEvent(new Event("dragstart", { bubbles: true }));
    });
    await act(async () => {
      hostRow.dispatchEvent(new Event("drop", { bubbles: true }));
      await Promise.resolve();
    });
    await flush();

    expect(reorderFleet).toHaveBeenCalledWith(["m1", "host-0", "m2"]);
  });

  it("disables every move control while a save is pending (busy)", async () => {
    mockMountReads();
    let release: () => void = () => {};
    const gate = new Promise<{ ok: boolean; order: string[] }>((resolve) => {
      release = () => resolve({ ok: true, order: [] });
    });
    vi.spyOn(api, "reorderFleet").mockReturnValue(gate);
    await renderPanel();

    await act(async () => {
      moveButton("Move alpha down")!.click();
      await Promise.resolve();
    });
    await flush();

    // The in-flight optimistic order still shows every row; all move controls are now busy-disabled.
    const buttons = moveButtons();
    expect(buttons).toHaveLength(6);
    expect(buttons.every((b) => b.disabled)).toBe(true);

    release();
    await flush();
  });

  it("reconciles through queryKeys.fleet on failure — rolls back without losing a row", async () => {
    mockMountReads();
    const reorderFleet = vi
      .spyOn(api, "reorderFleet")
      .mockRejectedValue(new Error("nope"));
    await renderPanel();

    await act(async () => {
      moveButton("Move alpha down")!.click();
      await Promise.resolve();
    });
    // Let the rejection route through onError (rollback) and onSettled (invalidate → refetch).
    await flush();
    await flush();
    await flush();

    expect(reorderFleet).toHaveBeenCalled();
    // Every row survived and the original order is restored — no member dropped, no order stuck.
    expect(moveButtons()).toHaveLength(6);
    expect(orderedUpLabels()).toEqual(["Move host up", "Move alpha up", "Move bravo up"]);
  });
});
