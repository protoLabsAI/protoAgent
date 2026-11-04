import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../lib/api";
import type { FleetAgent, FleetStatus } from "../lib/types";
import fleetCss from "../fleet/fleet.css?raw";
import fleetPanelSrc from "./FleetManagerPanel.tsx?raw";
import {
  BOX_RUNTIME_KEYS,
  canAddRemote,
  canMove,
  defaultGroupName,
  deleteGroupConfirm,
  dropBeforeIndex,
  fleetAutostartRoster,
  FleetGroupsPanel,
  fleetOrderIds,
  GROUP_NAME_MAX,
  groupHomeKey,
  groupNameError,
  groupPickerOptions,
  memberAutostarts,
  moveAnnouncement,
  moveInList,
  orderAgentsByIds,
  pluralAgents,
  reorderLabel,
  reorderByDrag,
  sameOrder,
  shouldSubmitOrder,
  slugOf,
  updateAutostartRoster,
} from "./FleetManagerPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The group editor branches on the mobile breakpoint (drag on/off, the picker's second line); a
// hoisted ref lets a test flip it, exactly as FleetSwitcher.test.tsx drives the switcher's.
const mobileRef = vi.hoisted(() => ({ value: false }));
vi.mock("../lib/useIsMobile", () => ({ useIsMobile: () => mobileRef.value }));

// Radix (the DS DropdownSelect + the ConfirmDialog focus-trap) touches layout + pointer-capture
// APIs jsdom lacks; stub them so the picker can open and the confirm can render — the same gap the
// sibling ProvidersPanel.ui / FleetSwitcher suites fill.
Element.prototype.hasPointerCapture ??= () => false;
Element.prototype.releasePointerCapture ??= () => {};
Element.prototype.setPointerCapture ??= () => {};
Element.prototype.scrollIntoView ??= () => {};
if (!(globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver) {
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
if (!window.matchMedia) {
  window.matchMedia = ((q: string) => ({
    matches: false,
    media: q,
    onchange: null,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() {
      return false;
    },
  })) as typeof window.matchMedia;
}

const GROUPS = [
  { id: "g-work", name: "Work" },
  { id: "g-personal", name: "Personal" },
];

// A fleet in switcher order: host (ungrouped), two more ungrouped, a member per group (one pinned),
// and a remote on a named machine. Enough to exercise every card and both the picker + star paths.
function roster(over: Partial<FleetStatus> = {}): FleetStatus {
  const a = (id: string, name: string, extra: Partial<FleetAgent> = {}): FleetAgent => ({
    id,
    name,
    port: 7800,
    pid: 1,
    running: false,
    bundle: "",
    ...extra,
  });
  return {
    groups: GROUPS,
    agents: [
      a("host-id", "atlas", { host: true, running: true }),
      a("n1", "nova", { group: "g-work" }),
      a("n2", "orion"),
      a("n3", "vega", { group: "g-personal", pinned: true, pinned_at: "2026-11-01T00:00:00Z" }),
      a("r1", "pulsar", { remote: true, machine: "mac-studio", running: true }),
    ],
    ...over,
  };
}

// A hub that predates the groups API (#4731): /api/fleet carries NO `groups` field at all.
function rosterNoGroups(): FleetStatus {
  return { agents: roster().agents }; // `groups` omitted entirely
}

let container: HTMLElement;
let root: Root;

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

// FleetGroupsPanel calls useToast() (its group writes toast on rejection), which throws outside a
// ToastProvider — so the editor renders inside one, and a rejected write's toast lands in the
// provider's `.pl-toast__title` / `.pl-toast__msg` where a test can read it.
function renderPanel(client: QueryClient) {
  act(() => {
    root.render(
      h(QueryClientProvider, { client }, h(ToastProvider, null, h(FleetGroupsPanel, { onBack: () => {} }))),
    );
  });
}

async function mount(status?: FleetStatus) {
  (api.fleet as ReturnType<typeof vi.fn>).mockResolvedValue(status ?? roster());
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderPanel(client);
  await flush();
  await flush();
}

// A write's isPending spans its onSettled → invalidate → refetch, so settling a mutation takes
// several hops; flush a few times when a later interaction depends on the prior write being done.
async function settle() {
  for (let i = 0; i < 4; i++) await flush();
}

const toastTitle = () => document.querySelector(".pl-toast__msg")?.textContent || "";
// The pin star for a named agent, whatever its current pinned/label state (its aria-label flips
// between "Pin"/"Unpin" as the row toggles, so find it by the row the name sits in).
const starFor = (name: string) =>
  [...document.querySelectorAll<HTMLElement>(".fleet-arow")]
    .find((li) => (li.textContent || "").includes(name))
    ?.querySelector(".fleet-star") as HTMLButtonElement | undefined;

const PointerCtor = (globalThis as unknown as { PointerEvent?: typeof MouseEvent }).PointerEvent ?? MouseEvent;
function pointer(type: string, el: Element, opts: MouseEventInit = {}) {
  el.dispatchEvent(new PointerCtor(type, { bubbles: true, cancelable: true, button: 0, ...opts }));
}
function clickEl(el: Element) {
  act(() => {
    pointer("pointerdown", el);
    pointer("pointerup", el);
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
  });
}
function setInput(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
function keydown(el: Element, key: string) {
  act(() => {
    el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
  });
}
// The DS DropdownSelect defers opening until click (#4534), and its options render as
// `menuitemradio`, not `option` (the ProvidersPanel.ui recipe).
function openPicker(label: string) {
  const trigger = document.querySelector(`[aria-label="${label}"]`) as HTMLElement;
  act(() => pointer("pointerdown", trigger, { pointerType: "touch" } as MouseEventInit));
  act(() => {
    pointer("pointerup", trigger, { pointerType: "touch" } as MouseEventInit);
    trigger.click();
  });
}
const pickerItem = (text: string) =>
  [...document.querySelectorAll<HTMLElement>('[role="menuitemradio"]')].find((o) => (o.textContent || "").trim() === text);
const byText = (text: string) => [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === text);
const savedText = () => document.querySelector(".fleet-saved")?.textContent || "";

beforeEach(() => {
  mobileRef.value = false;
  vi.spyOn(api, "fleet").mockResolvedValue(roster());
  vi.spyOn(api, "createFleetGroup").mockResolvedValue({ ok: true, group: { id: "g-new", name: "New group" } });
  vi.spyOn(api, "renameFleetGroup").mockResolvedValue({ ok: true, group: { id: "g-work", name: "QA" } });
  vi.spyOn(api, "reorderFleetGroups").mockResolvedValue({ ok: true, groups: GROUPS });
  vi.spyOn(api, "deleteFleetGroup").mockResolvedValue({ ok: true, id: "g-work", removed: false, ungrouped: 1 });
  vi.spyOn(api, "setFleetAgentGroup").mockResolvedValue({ ok: true, id: "x", group: null });
  vi.spyOn(api, "pinFleetAgent").mockResolvedValue({ ok: true, id: "x", pinned: true, pinned_at: "" });
  vi.spyOn(api, "unpinFleetAgent").mockResolvedValue({ ok: true, id: "x", pinned: false });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

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
// The API payload, the drag / arrow-key math, the boundaries, the handle's accessible name, its
// live-region announcement and the failure-safe reconciliation are all pure, so they're unit-
// tested without rendering the panel (the console has no @testing-library; this suite is
// `.test.ts` only). The rendered interaction — a real drag, a real ↑ / ↓ press, and the order
// surviving a reload — is covered end-to-end in e2e/fleet.spec.ts.

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

describe("moveInList — the ↑ / ↓ keyboard payload", () => {
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

describe("dropBeforeIndex — a group drag lands BEFORE the drop-before marker", () => {
  const ids = ["A", "B", "C"];
  // The group editor drops onto a target card's "before" marker: reorderByDrag removes the dragged id
  // first, so a FORWARD move shifts the target one slot left — the index must be nudged back by one.
  const dropBefore = (from: number, to: number) => reorderByDrag(ids, from, dropBeforeIndex(from, to));

  it("nudges a forward drop back one slot, backward/same drops are unchanged", () => {
    expect(dropBeforeIndex(0, 2)).toBe(1); // forward: A before C → insert at 1
    expect(dropBeforeIndex(2, 0)).toBe(0); // backward: C before A → insert at 0
    expect(dropBeforeIndex(1, 1)).toBe(1); // same slot
  });

  it("drops A onto C's before-marker → [B, A, C], not [B, C, A]", () => {
    // Without the nudge, removing A shifts C to index 1 and A lands AFTER it ([B, C, A]).
    expect(dropBefore(0, 2)).toEqual(["B", "A", "C"]);
  });

  it("a backward drop (C before A) is unaffected → [C, A, B]", () => {
    expect(dropBefore(2, 0)).toEqual(["C", "A", "B"]);
  });
});

describe("canMove — the list boundaries the keyboard path announces", () => {
  it("is false for ↑ at the top and ↓ at the bottom", () => {
    expect(canMove(0, "up", 3)).toBe(false);
    expect(canMove(1, "up", 3)).toBe(true);
    expect(canMove(2, "down", 3)).toBe(false);
    expect(canMove(1, "down", 3)).toBe(true);
  });

  it("a member just below the pinned host can still move up (full-array indices)", () => {
    // host at 0, first member at 1 → ↑ is a real move and swaps it above the host.
    expect(canMove(1, "up", 3)).toBe(true);
  });
});

describe("reorderLabel — the handle's accessible name", () => {
  it("names the DISPLAY name (label ?? name), never the id", () => {
    expect(reorderLabel({ name: "ava", label: "Ava 🚀" })).toContain("Ava 🚀");
    expect(reorderLabel({ name: "roxy" })).toContain("roxy");
  });

  it("carries the keys, because no visible control does any more", () => {
    // The move-up / move-down buttons are gone; the handle's name is where a keyboard user
    // learns the path exists at all (with aria-keyshortcuts + the tooltip).
    expect(reorderLabel({ name: "roxy" })).toMatch(/up and down arrow keys/);
  });
});

describe("moveAnnouncement — the keyboard path's polite live region", () => {
  it("reports the DESTINATION slot, one-based, of the whole roster", () => {
    // roster host, ava, roxy — ↓ on ava (index 1) lands it in the third slot, ↑ in the first.
    expect(moveAnnouncement({ name: "ava" }, 1, "down", 3)).toBe("Moved ava to position 3 of 3.");
    expect(moveAnnouncement({ name: "ava" }, 1, "up", 3)).toBe("Moved ava to position 1 of 3.");
  });

  it("names the boundary instead of going silent on a no-op press", () => {
    // A press that cannot move is the one case with NO visual change at all — the old UI
    // showed it as a disabled button; now only this line carries it.
    expect(moveAnnouncement({ name: "roxy" }, 0, "up", 3)).toBe("roxy is already first.");
    expect(moveAnnouncement({ name: "roxy" }, 2, "down", 3)).toBe("roxy is already last.");
  });

  it("uses the display label when there is one", () => {
    expect(moveAnnouncement({ name: "ava", label: "Ava 🚀" }, 1, "up", 3)).toContain("Ava 🚀");
  });
});

describe("sameOrder — skip a no-op PUT", () => {
  it("is true only for an element-wise identical order", () => {
    expect(sameOrder(["a", "b"], ["a", "b"])).toBe(true);
    expect(sameOrder(["a", "b"], ["b", "a"])).toBe(false);
    expect(sameOrder(["a"], ["a", "b"])).toBe(false);
  });
});

describe("shouldSubmitOrder — the single guard shared by the drag AND keyboard paths", () => {
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

// ── Fleet settings group editor (docs/design/fleet-switcher-groups.md) — the pure core ─────────
// The name rules + their copy, the delete confirm's wording, the picker options, the per-agent
// home key and the count label are pure, so they're unit-tested without the DOM — exactly as the
// roster-reorder core above is. Copy is verbatim from the spec (and matches the switcher's).

describe("groupNameError — the spec's rename rules + error copy", () => {
  const groups = [
    { id: "g-work", name: "Work" },
    { id: "g-personal", name: "Personal" },
  ];

  it("accepts a fresh, trimmed, in-range name", () => {
    expect(groupNameError("Demos", groups)).toBeNull();
    expect(groupNameError("  Demos  ", groups)).toBeNull();
  });

  it("rejects a blank name with the spec's copy", () => {
    expect(groupNameError("", groups)).toBe("Give the group a name.");
    expect(groupNameError("   ", groups)).toBe("Give the group a name.");
  });

  it("reserves 'Remote' (case-insensitive) for paired machines", () => {
    expect(groupNameError("Remote", groups)).toBe('"Remote" is reserved for agents on paired machines.');
    expect(groupNameError("remote", groups)).toBe('"Remote" is reserved for agents on paired machines.');
  });

  it("rejects a duplicate (case-insensitive) with the name echoed back", () => {
    expect(groupNameError("work", groups)).toBe('There\'s already a group called "work".');
    expect(groupNameError("Personal", groups)).toBe('There\'s already a group called "Personal".');
  });

  it("excludes the group being renamed from the duplicate check (a no-op rename is allowed)", () => {
    expect(groupNameError("Work", groups, "g-work")).toBeNull();
    // …but it still clashes with a DIFFERENT group.
    expect(groupNameError("Personal", groups, "g-work")).toBe('There\'s already a group called "Personal".');
  });

  it("caps the field at 40 characters (spec: 1–40)", () => {
    expect(GROUP_NAME_MAX).toBe(40);
  });
});

describe("pluralAgents — the card count + confirm label", () => {
  it("is singular only for one", () => {
    expect(pluralAgents(0)).toBe("0 agents");
    expect(pluralAgents(1)).toBe("1 agent");
    expect(pluralAgents(5)).toBe("5 agents");
  });
});

describe("deleteGroupConfirm — the spec's confirm copy (verbatim from the approved preview)", () => {
  it("names the group in the title and says the agents move to No group, none removed", () => {
    expect(deleteGroupConfirm("Work", 3)).toEqual({
      title: 'Delete the group "Work"?',
      body: "Its 3 agents move to No group. No agents are removed.",
    });
  });

  it("uses the singular count AND verb for one agent (1 agent moves, N agents move)", () => {
    expect(deleteGroupConfirm("Work", 1).body).toBe("Its 1 agent moves to No group. No agents are removed.");
    expect(deleteGroupConfirm("Work", 2).body).toBe("Its 2 agents move to No group. No agents are removed.");
  });

  it("reads differently for an empty group", () => {
    expect(deleteGroupConfirm("Work", 0).body).toBe("It has no agents. No agents are removed.");
  });
});

describe("groupPickerOptions — the Group picker (No group, then each group in order)", () => {
  it("leads with No group (empty value) then the groups in switcher order", () => {
    expect(groupPickerOptions(GROUPS_FIXTURE)).toEqual([
      { value: "", label: "No group" },
      { value: "g-work", label: "Work" },
      { value: "g-personal", label: "Personal" },
    ]);
  });
});
const GROUPS_FIXTURE = [
  { id: "g-work", name: "Work" },
  { id: "g-personal", name: "Personal" },
];

describe("groupHomeKey — which editor card an agent lives in", () => {
  const ids = new Set(["g-work", "g-personal"]);
  it("sends remote agents to the Remote card regardless of any group field", () => {
    expect(groupHomeKey({ remote: true, group: "g-work" }, ids)).toBe("remote");
  });
  it("uses the group when it still exists, else No group", () => {
    expect(groupHomeKey({ group: "g-work" }, ids)).toBe("g-work");
    expect(groupHomeKey({ group: "g-gone" }, ids)).toBe("ungrouped"); // a stale id is ungrouped
    expect(groupHomeKey({ group: null }, ids)).toBe("ungrouped");
    expect(groupHomeKey({}, ids)).toBe("ungrouped");
  });
});

describe("defaultGroupName — Add group lands a unique, overtypeable name", () => {
  it("is 'New group' when free, else the first unused 'New group N'", () => {
    expect(defaultGroupName([])).toBe("New group");
    expect(defaultGroupName([{ id: "a", name: "New group" }])).toBe("New group 2");
    expect(defaultGroupName([
      { id: "a", name: "New group" },
      { id: "b", name: "New group 2" },
    ])).toBe("New group 3");
  });
});

// ── FleetGroupsPanel — the rendered editor (createRoot/act + QueryClientProvider, the console UI
// idiom; the DS Menu/DropdownSelect/ConfirmDialog portal to <body>, so queried from `document`). ──
describe("FleetGroupsPanel — desktop group cards", () => {
  it("renders a card per group in switcher order, then No group and Remote, with the help + counts", async () => {
    await mount();
    const text = container.textContent || "";
    expect(text).toContain("Groups organize the agent switcher. They don't change what agents can do.");
    // Cards in order: Work, Personal, then the fixed No group + Remote.
    const order = ["Work", "Personal", "No group", "Remote"];
    const pos = order.map((label) => text.indexOf(label));
    expect(pos.every((p) => p >= 0)).toBe(true);
    expect(pos).toEqual([...pos].sort((x, y) => x - y));
    // The read-only Remote card carries its automatic note and the machine name.
    expect(text).toContain("Agents from paired machines are grouped automatically.");
    expect(text).toContain("On mac-studio");
    // The No group card's heading note.
    expect(text).toContain("These sit near the top of the switcher, without a heading.");
    // Count badges: Work has nova (1).
    const work = document.querySelector('[aria-label="Work group"]')!;
    expect(work.textContent).toContain("1 agent");
  });

  it("adds a group (default name) and saves live", async () => {
    await mount();
    clickEl(byText("Add group")!);
    await flush();
    expect(api.createFleetGroup).toHaveBeenCalledWith("New group");
    expect(savedText()).toBe("Saved"); // the inline live-save note
  });

  it("renames inline: Enter saves the new name, a no-op rename hits nothing", async () => {
    await mount();
    clickEl([...document.querySelectorAll<HTMLElement>(".fleet-gname-btn")].find((b) => b.textContent === "Work")!);
    await flush();
    const input = document.querySelector('input[aria-label="Group name"]') as HTMLInputElement;
    expect(input.value).toBe("Work");
    setInput(input, "QA");
    keydown(input, "Enter");
    await flush();
    expect(api.renameFleetGroup).toHaveBeenCalledWith("g-work", "QA");
    expect(savedText()).toBe("Saved");
  });

  it("shows the spec's rename errors (blank, reserved, duplicate) and never writes", async () => {
    await mount();
    clickEl([...document.querySelectorAll<HTMLElement>(".fleet-gname-btn")].find((b) => b.textContent === "Work")!);
    await flush();
    const input = document.querySelector('input[aria-label="Group name"]') as HTMLInputElement;
    const err = () => document.querySelector(".fleet-grename-err")?.textContent || "";

    setInput(input, "   ");
    keydown(input, "Enter");
    await flush();
    expect(err()).toBe("Give the group a name.");

    setInput(input, "Remote");
    keydown(input, "Enter");
    await flush();
    expect(err()).toBe('"Remote" is reserved for agents on paired machines.');

    setInput(input, "Personal");
    keydown(input, "Enter");
    await flush();
    expect(err()).toBe('There\'s already a group called "Personal".');

    expect(api.renameFleetGroup).not.toHaveBeenCalled(); // every attempt was rejected locally
  });

  it("cancels an inline rename on Escape without writing", async () => {
    await mount();
    clickEl([...document.querySelectorAll<HTMLElement>(".fleet-gname-btn")].find((b) => b.textContent === "Work")!);
    await flush();
    const input = document.querySelector('input[aria-label="Group name"]') as HTMLInputElement;
    setInput(input, "QA");
    keydown(input, "Escape");
    await flush();
    expect(document.querySelector('input[aria-label="Group name"]')).toBeFalsy(); // back to the label
    expect(api.renameFleetGroup).not.toHaveBeenCalled();
  });

  it("reorders with Move up / Move down, disables them at the boundaries, and saves the full order", async () => {
    await mount();
    // Work is first → its Move up is disabled; Personal is last → its Move down is disabled.
    const workUp = document.querySelector('[data-gmove="up"][data-gid="g-work"]') as HTMLButtonElement;
    const personalDown = document.querySelector('[data-gmove="down"][data-gid="g-personal"]') as HTMLButtonElement;
    expect(workUp.disabled).toBe(true);
    expect(personalDown.disabled).toBe(true);
    // Move Work down → the complete group-id permutation is PUT.
    clickEl(document.querySelector('[data-gmove="down"][data-gid="g-work"]')!);
    await flush();
    expect(api.reorderFleetGroups).toHaveBeenCalledWith(["g-personal", "g-work"]);
    expect(savedText()).toBe("Saved");
  });

  it("deletes a group through the spec's confirm", async () => {
    await mount();
    clickEl(document.querySelector('[aria-label="Delete Work"]')!);
    await flush();
    const dialog = document.querySelector('[role="alertdialog"], [role="dialog"]')!;
    expect(dialog.textContent).toContain('Delete the group "Work"?');
    // Work holds one agent (nova), so the confirm uses the singular verb "moves".
    expect(dialog.textContent).toContain("Its 1 agent moves to No group. No agents are removed.");
    clickEl(byText("Delete group")!); // the destructive confirm button
    await flush();
    expect(api.deleteFleetGroup).toHaveBeenCalledWith("g-work");
  });

  it("assigns an agent from its row Group picker", async () => {
    await mount();
    openPicker("Group for orion"); // orion is ungrouped
    await flush();
    clickEl(pickerItem("Work")!);
    await flush();
    expect(api.setFleetAgentGroup).toHaveBeenCalledWith("n2", "g-work");
    expect(savedText()).toBe("Saved");
  });

  it("pins and unpins any agent — the host, a member and a remote", async () => {
    await mount();
    clickEl(document.querySelector('[aria-label="Pin nova"]')!);
    await flush();
    expect(api.pinFleetAgent).toHaveBeenCalledWith("n1");
    clickEl(document.querySelector('[aria-label="Unpin vega"]')!); // vega starts pinned
    await flush();
    expect(api.unpinFleetAgent).toHaveBeenCalledWith("n3");
    clickEl(document.querySelector('[aria-label="Pin pulsar"]')!); // a remote can be pinned
    await flush();
    expect(api.pinFleetAgent).toHaveBeenCalledWith("r1");
  });

  it("leaves Remote read-only: the machine shows, there is no group picker, the star stays", async () => {
    await mount();
    const remoteCard = document.querySelector('[aria-label="Remote"]')!;
    expect(remoteCard.textContent).toContain("On mac-studio");
    expect(document.querySelector('[aria-label="Group for pulsar"]')).toBeFalsy(); // no picker
    expect(document.querySelector('[aria-label="Pin pulsar"]')).toBeTruthy(); // but still pinnable
    // The star carries aria-pressed so it reads as a toggle.
    expect(document.querySelector('[aria-label="Pin pulsar"]')!.getAttribute("aria-pressed")).toBe("false");
    expect(document.querySelector('[aria-label="Unpin vega"]')!.getAttribute("aria-pressed")).toBe("true");
  });

  it("wires the desktop drag path: member rows + card handles drag, remote rows do not", async () => {
    await mount();
    expect(document.querySelectorAll(".fleet-arow[draggable='true']").length).toBeGreaterThan(0);
    expect(document.querySelectorAll(".fleet-ghandle").length).toBe(2); // one per operator group
    // The remote row is not draggable.
    const remoteCard = document.querySelector('[aria-label="Remote"]')!;
    expect(remoteCard.querySelector(".fleet-arow[draggable='true']")).toBeFalsy();
  });
});

// ── PR #4735 review fixes: optimistic writes land via onMutate (cancel-first), overlapping writes of
// one kind are refused at the source, a rejected write is reported, and a failed initial load says so.
describe("FleetGroupsPanel — write behaviour (PR #4735 review)", () => {
  // Hold a write open so a later interaction sees it unambiguously in flight (its isPending otherwise
  // spans the settle refetch; a never-resolving mock removes that timing from the test).
  function hang<T>() {
    let release!: (v: T) => void;
    const promise = new Promise<T>((res) => {
      release = res;
    });
    return { promise, release };
  }

  it("optimistically moves a picked agent via onMutate, cancelling the in-flight poll first (:1191)", async () => {
    const h1 = hang<{ ok: true; id: string; group: string | null }>();
    (api.setFleetAgentGroup as ReturnType<typeof vi.fn>).mockReturnValue(h1.promise);
    const cancelSpy = vi.spyOn(QueryClient.prototype, "cancelQueries");
    await mount();
    openPicker("Group for orion"); // orion (n2) is ungrouped
    await flush();
    clickEl(pickerItem("Work")!);
    await settle();
    // The onMutate patch moved orion into the Work card before the write settled…
    expect(document.querySelector('[aria-label="Work group"]')!.textContent).toContain("orion");
    // …and it cancelled the in-flight fleet poll first (only optimisticPatch calls cancelQueries).
    expect(cancelSpy).toHaveBeenCalled();
    h1.release({ ok: true, id: "n2", group: "g-work" });
  });

  it("ignores a second group pick while the first assign is still in flight (:1293)", async () => {
    const h1 = hang<{ ok: true; id: string; group: string | null }>();
    (api.setFleetAgentGroup as ReturnType<typeof vi.fn>).mockReturnValue(h1.promise);
    await mount();
    openPicker("Group for orion");
    await flush();
    clickEl(pickerItem("Work")!); // assign #1 → in flight (never settles)
    await settle();
    expect(api.setFleetAgentGroup).toHaveBeenCalledTimes(1);
    // Pick again while the first write is pending — the isPending guard drops it, so no second PUT.
    openPicker("Group for orion");
    await flush();
    const personal = pickerItem("Personal");
    if (personal) clickEl(personal);
    await settle();
    expect(api.setFleetAgentGroup).toHaveBeenCalledTimes(1);
    h1.release({ ok: true, id: "n2", group: "g-work" });
  });

  it("disables only the pressed row's pin star while its own write is pending — no double-toggle (:1301)", async () => {
    const h1 = hang<{ ok: true; id: string; pinned: boolean; pinned_at: string }>();
    (api.pinFleetAgent as ReturnType<typeof vi.fn>).mockReturnValue(h1.promise);
    await mount();
    clickEl(starFor("nova")!); // pin nova → in flight
    expect(starFor("nova")!.disabled).toBe(true); // its own write is pending → no re-toggle
    expect(starFor("orion")!.disabled).toBe(false); // a different row's star stays live
    clickEl(starFor("nova")!); // a second press is ignored (disabled)
    await settle();
    expect(api.pinFleetAgent).toHaveBeenCalledTimes(1);
    h1.release({ ok: true, id: "n1", pinned: true, pinned_at: "" });
  });

  it("shows the panel's error toast when a group write is rejected (:1202)", async () => {
    (api.setFleetAgentGroup as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("server said no"));
    await mount();
    openPicker("Group for orion");
    await flush();
    clickEl(pickerItem("Work")!);
    await settle();
    expect(toastTitle()).toContain("Couldn't save that change. Try again.");
  });

  it("shows the error toast when creating a group is rejected (:1202, addGroup no longer silent)", async () => {
    (api.createFleetGroup as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("name taken"));
    await mount();
    clickEl(byText("Add group")!);
    await settle();
    expect(toastTitle()).toContain("Couldn't save that change. Try again.");
  });

  it("a press that begins on the pin star does not arm a row drag (:1362)", async () => {
    await mount();
    const star = starFor("nova")!;
    const row = star.closest(".fleet-arow") as HTMLElement;
    expect(row.getAttribute("draggable")).toBe("true"); // a press on the row body still drags it
    act(() => pointer("pointerdown", star)); // …but a press that starts on the star…
    expect(row.getAttribute("draggable")).not.toBe("true"); // …drops the row's draggable, so no drag starts
    act(() => pointer("pointerup", star));
    expect(row.getAttribute("draggable")).toBe("true"); // and it rearms on release
  });

  it("shows a load error instead of the empty state when the initial fleet read fails (:1516)", async () => {
    (api.fleet as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("boom"));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderPanel(client);
    await settle();
    expect(container.textContent).toContain("Couldn't load the fleet");
    expect(container.textContent).not.toContain("No groups yet"); // the empty editor is NOT shown
    expect(byText("Add group")).toBeFalsy(); // and there is nothing to add into
  });
});

// ── This card: a hub without the groups API shows an update note; a rejected write rolls back and
// is reported, never swallowed by a silent catch. ──
describe("FleetGroupsPanel — hub without the groups API (#4731)", () => {
  const UPDATE_NOTE =
    "Groups need a newer protoAgent on this machine. Update protoAgent, then come back here to set up groups.";

  it("shows the update note instead of the editor, with no Add group and no group cards", async () => {
    await mount(rosterNoGroups());
    expect(container.textContent).toContain(UPDATE_NOTE);
    expect(document.querySelector(".fleet-groups")).toBeFalsy(); // the editor body is not rendered
    expect(byText("Add group")).toBeFalsy();
    expect(document.querySelector('[aria-label="Work group"]')).toBeFalsy();
  });
});

describe("FleetGroupsPanel — a rejected write rolls back and shows the error copy (this card)", () => {
  const GENERIC = "Couldn't save that change. Try again.";

  it("rolls a rejected assign back to its card and shows the generic copy", async () => {
    (api.setFleetAgentGroup as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("405 method not allowed"));
    await mount();
    openPicker("Group for orion"); // orion (n2) is ungrouped
    await flush();
    clickEl(pickerItem("Work")!);
    await settle();
    expect(toastTitle()).toContain(GENERIC);
    // Rolled back: orion is NOT in Work — it is back in No group.
    expect(document.querySelector('[aria-label="Work group"]')!.textContent).not.toContain("orion");
    expect(document.querySelector('[aria-label="No group"]')!.textContent).toContain("orion");
  });

  it("rolls a rejected pin back to unpinned and shows the generic copy", async () => {
    (api.pinFleetAgent as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("405 method not allowed"));
    await mount();
    clickEl(starFor("nova")!); // nova starts unpinned
    await settle();
    expect(toastTitle()).toContain(GENERIC);
    // Rolled back: nova is unpinned again (the star offers Pin, aria-pressed false).
    const star = starFor("nova")!;
    expect(star.getAttribute("aria-label")).toBe("Pin nova");
    expect(star.getAttribute("aria-pressed")).toBe("false");
  });

  it("surfaces the hub's own message when a rename is rejected (a 409 duplicate from another browser)", async () => {
    (api.renameFleetGroup as ReturnType<typeof vi.fn>).mockRejectedValue(
      new ApiError(409, 'There\'s already a group called "QA".'),
    );
    await mount();
    clickEl([...document.querySelectorAll<HTMLElement>(".fleet-gname-btn")].find((b) => b.textContent === "Work")!);
    await flush();
    const input = document.querySelector('input[aria-label="Group name"]') as HTMLInputElement;
    setInput(input, "QA"); // clears the local rules; the hub still rejects it
    keydown(input, "Enter");
    await settle();
    expect(api.renameFleetGroup).toHaveBeenCalledWith("g-work", "QA");
    expect(toastTitle()).toContain('There\'s already a group called "QA".');
    // The rename was never applied optimistically — the card keeps its old name.
    expect(document.querySelector('[aria-label="Work group"]')).toBeTruthy();
  });

  it("never swallows a failed fleet write in an empty catch block (addGroup's `catch {}` is gone)", () => {
    // The bug this card fixes was a silent `catch {}`. Guard the source so one can't creep back.
    expect(fleetPanelSrc).not.toMatch(/catch\s*(\([^)]*\))?\s*\{\s*\}/);
  });
});

describe("FleetGroupsPanel — phone (one column, drag off, 44px targets)", () => {
  beforeEach(() => {
    mobileRef.value = true;
  });

  it("drops the drag affordances and marks the phone layout", async () => {
    await mount();
    expect(document.querySelector(".fleet-groups.is-phone")).toBeTruthy();
    expect(document.querySelectorAll(".fleet-ghandle").length).toBe(0); // no drag handles on touch
    expect(document.querySelectorAll(".fleet-arow[draggable='true']").length).toBe(0); // rows not draggable
    // The picker is still the assign path on touch.
    expect(document.querySelector('[aria-label="Group for orion"]')).toBeTruthy();
    // 44px targets ship in the stylesheet (jsdom computes no layout).
    expect(fleetCss).toContain("min-height: 44px");
    expect(fleetCss).toContain(".fleet-groups.is-phone .fleet-ghandle");
  });
});

describe("FleetGroupsPanel — gated like the rest of Fleet settings", () => {
  it("is read-only in a directly-reached member window (no cards, no Add group)", async () => {
    // The host entry self-reports `member: true` — a spawned member reached directly on its own
    // port, where there is no hub roster to write to (fleetSettingsDisabledReason).
    await mount(
      roster({
        agents: [
          { id: "host-id", name: "atlas", port: 7800, pid: 1, running: true, bundle: "", host: true, member: true },
          { id: "n1", name: "nova", port: 7801, pid: 2, running: false, bundle: "", group: "g-work" },
        ],
      }),
    );
    expect(container.textContent).toContain("Fleet settings are managed from the host instance");
    expect(document.querySelector(".fleet-groups")).toBeFalsy(); // the editor body is not rendered
    expect(byText("Add group")).toBeFalsy();
  });
});
