// FleetSwitcher — desktop dropdown + phone bottom sheet (docs/design/fleet-switcher-groups.md).
// Renders the REAL component against the REAL DS Menu/MenuGroup/MenuFilter/Drawer (@protolabsai/ui
// ^0.69) with a mocked `api`, in the createRoot/act + QueryClientProvider idiom the other console
// UI suites use (FleetRoom.test.tsx). The DS Menu and Drawer portal to <body>, so open content is
// queried from `document`, not the mount container.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FleetAgent, FleetStatus } from "../lib/types";
import { FleetSwitcher } from "./FleetSwitcher";
import switcherSrc from "./FleetSwitcher.tsx?raw";
import themeCss from "./theme.css?raw";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// A controllable breakpoint — the switcher branches on it (desktop Menu vs phone Drawer).
const mobileRef = vi.hoisted(() => ({ value: false }));
vi.mock("../lib/useIsMobile", () => ({ useIsMobile: () => mobileRef.value }));

vi.mock("../lib/api", () => {
  // The real ApiError (carries the server's `detail` as its message). The component does
  // `e instanceof ApiError` to decide whether to surface the server's copy, so the mock must
  // export the SAME class the test rejects with for that check to hold.
  class ApiError extends Error {
    constructor(
      readonly status: number,
      message: string,
      readonly code?: string,
    ) {
      super(message);
      this.name = "ApiError";
    }
  }
  return {
    ApiError,
    agentHref: vi.fn((slug: string) => `/app/agent/${slug}/`),
    currentSlug: vi.fn(() => "host"),
    api: {
      fleet: vi.fn(),
      pinFleetAgent: vi.fn(),
      unpinFleetAgent: vi.fn(),
      setFleetAgentGroup: vi.fn(),
      createFleetGroup: vi.fn(),
    },
  };
});

import { agentHref, api, ApiError, currentSlug } from "../lib/api";

// Radix (DropdownMenu/Popper/Drawer focus-trap) touches layout + pointer-capture APIs jsdom lacks.
Element.prototype.hasPointerCapture ??= () => false;
Element.prototype.releasePointerCapture ??= () => {};
Element.prototype.setPointerCapture ??= () => {};
const scrollIntoView = vi.fn();
Element.prototype.scrollIntoView = scrollIntoView;
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
  { id: "g-demos", name: "Demos" },
  { id: "g-personal", name: "Personal" },
];

function roster(): FleetStatus {
  const a = (
    id: string,
    name: string,
    over: Partial<FleetAgent> = {},
  ): FleetAgent => ({ id, name, port: 7800, pid: 1, running: false, bundle: "", ...over });
  return {
    groups: GROUPS,
    agents: [
      a("host-id", "atlas", { host: true, running: true }),
      a("n1", "nova", { running: true }),
      a("n2", "orion"),
      a("n7", "sol", { pinned: true, pinned_at: "2026-11-02T00:00:00Z" }),
      a("n8", "cass"),
      a("n9", "deneb"),
      a("n3", "vega", { group: "g-demos" }),
      a("n4", "lyra", { group: "g-demos", running: true }),
      a("n5", "mira", { group: "g-personal" }),
      a("n6", "rigel", { group: "g-personal", pinned: true, pinned_at: "2026-11-01T00:00:00Z" }),
      a("r1", "pulsar", { remote: true, machine: "mac-studio", running: true }),
      a("r2", "quasar", { remote: true, machine: "mac-mini" }),
    ],
  };
}

// A hub that predates the groups API (#4731): /api/fleet carries NO `groups` field at all (all the
// switcher fields are optional by design). The leftover `group` ids on agents point at groups the
// roster no longer lists, so every one of them falls back to Ungrouped — the plain list.
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

async function mount(actions: { onNewAgent?: () => void; onManageFleet?: () => void } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => {
    // The switcher toasts a rejected pin/move (it no longer fails silently), so it renders inside a
    // ToastProvider — as it does in the app (AppToasts wraps <App/>). useToast() throws without one.
    root.render(
      h(QueryClientProvider, { client }, h(ToastProvider, null, h(FleetSwitcher, { fallbackName: "protoagent", ...actions }))),
    );
  });
  await flush();
}
// A few flushes: a rejected write settles through onError → rollback → onSettled invalidate → refetch.
async function settle() {
  for (let i = 0; i < 4; i++) await flush();
}
const toastText = () => document.querySelector(".pl-toast__msg")?.textContent || "";

function pointer(type: string, el: Element) {
  const Ctor = (globalThis as unknown as { PointerEvent?: typeof MouseEvent }).PointerEvent ?? MouseEvent;
  el.dispatchEvent(new Ctor(type, { bubbles: true, cancelable: true, button: 0 } as MouseEventInit));
}
function clickEl(el: Element) {
  act(() => {
    pointer("pointerdown", el);
    pointer("pointerup", el);
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
  });
}
// A bare click — used for the DS MenuItem `action` (the pin star), whose own handlers fire on
// click and stop propagation; dispatching Radix's pointerup here would instead select the row.
function clickOnly(el: Element) {
  act(() => {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
  });
}
async function openMenu() {
  const trigger = document.querySelector('[data-testid="fleet-switcher"]')!;
  clickEl(trigger);
  await flush();
}
// The open switcher content (NOT the row-actions menu, which also carries `.pl-menu`).
const mainMenu = () => document.querySelector(".pl-menu:not(.fleet-rowmenu)");
const rowMenu = () => document.querySelector(".pl-menu.fleet-rowmenu");
const itemByText = (scope: Element | null, text: string) =>
  [...(scope?.querySelectorAll('[role="menuitem"],[role="menuitemradio"],[role="menuitemcheckbox"]') ?? [])].find((el) =>
    (el.textContent || "").includes(text),
  ) as HTMLElement | undefined;

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

beforeEach(() => {
  mobileRef.value = false;
  vi.mocked(currentSlug).mockReturnValue("host");
  localStorage.clear();
  scrollIntoView.mockClear();
  (api.fleet as ReturnType<typeof vi.fn>).mockResolvedValue(roster());
  (api.pinFleetAgent as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, id: "x", pinned: true, pinned_at: "" });
  (api.unpinFleetAgent as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, id: "x", pinned: false });
  (api.setFleetAgentGroup as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, id: "x", group: null });
  (api.createFleetGroup as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, group: { id: "g-qa", name: "QA" } });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.body.style.overflow = "";
  vi.clearAllMocks();
});

describe("FleetSwitcher — desktop dropdown", () => {
  it("renders the sections in the spec's order with the spec's copy", async () => {
    localStorage.setItem("protoagent.fleet.switcherRecent", JSON.stringify(["n1"])); // nova was switched to earlier
    await mount();
    await openMenu();
    const t = mainMenu()!.textContent || "";
    const order = ["Pinned", "Demos", "Personal", "Remote"];
    const positions = order.map((label) => t.indexOf(label));
    expect(positions.every((p) => p >= 0)).toBe(true);
    expect(positions).toEqual([...positions].sort((x, y) => x - y));
    // Filter box is present (≥ 10 agents) with the spec's accessible name + placeholder.
    expect(document.querySelector('[aria-label="Find an agent by name or group"]')).toBeTruthy();
    expect(document.querySelector('input[placeholder="Find an agent"]')).toBeTruthy();
    // A remote row carries its machine Badge.
    expect(t).toContain("mac-studio");
  });

  it("filters across all groups (opening a collapsed one) and Enter switches to the top match", async () => {
    localStorage.setItem("protoagent.fleet.collapsed", JSON.stringify(["g-demos"])); // Demos starts collapsed
    await mount();
    await openMenu();
    const input = document.querySelector('input[placeholder="Find an agent"]') as HTMLInputElement;
    setInput(input, "lyra"); // lyra lives in the collapsed Demos group
    await flush();
    const t = mainMenu()!.textContent || "";
    expect(t).toContain("lyra"); // collapsed group opened temporarily to show the match
    expect(t).not.toContain("nova"); // non-matches hidden
    expect(t).not.toContain("Pinned"); // no pinned agents match lyra
    keydown(input, "Enter");
    await flush();
    expect(agentHref).toHaveBeenCalledWith("n4");
    expect(localStorage.getItem("protoagent.fleet.switcherRecent")).toBeNull();
    expect(localStorage.getItem("protoagent.fleet.recent")).toBeNull();
  });

  it("shows a disabled no-match row with the spec's copy", async () => {
    await mount();
    await openMenu();
    const input = document.querySelector('input[placeholder="Find an agent"]') as HTMLInputElement;
    setInput(input, "zzzzz");
    await flush();
    expect(mainMenu()!.textContent).toContain('No agents match "zzzzz"');
  });

  it("persists collapse state per browser when a group header is toggled", async () => {
    await mount();
    await openMenu();
    const demos = itemByText(mainMenu(), "Demos");
    expect(demos).toBeTruthy();
    clickEl(demos!);
    await flush();
    expect(JSON.parse(localStorage.getItem("protoagent.fleet.collapsed") || "[]")).toContain("g-demos");
  });

  it("ignores stored recents", async () => {
    localStorage.setItem("protoagent.fleet.switcherRecent", JSON.stringify(["n2"])); // orion
    await mount();
    await openMenu();
    const t = mainMenu()!.textContent || "";
    expect(t).not.toContain("Recent");
    expect(t).toContain("orion");
  });

  it("pins and unpins from the row's star action (pins come from the fleet API)", async () => {
    await mount();
    await openMenu();
    clickOnly(document.querySelector('[aria-label="Pin nova to top"]')!);
    await flush();
    expect(api.pinFleetAgent).toHaveBeenCalledWith("n1");
    clickOnly(document.querySelector('[aria-label="Unpin sol"]')!);
    await flush();
    expect(api.unpinFleetAgent).toHaveBeenCalledWith("n7");
  });

  it("moves an agent to an existing group from the row menu", async () => {
    await mount();
    await openMenu();
    const nameSpan = [...document.querySelectorAll(".fleet-switcher-name")].find((el) =>
      (el.textContent || "").includes("nova"),
    )!;
    act(() => nameSpan.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
    await flush();
    clickEl(itemByText(rowMenu(), "Move to group…")!);
    await flush();
    clickEl(itemByText(rowMenu(), "Demos")!);
    await flush();
    expect(api.setFleetAgentGroup).toHaveBeenCalledWith("n1", "g-demos");
  });

  it("creates a new group and moves the agent into it (New group…)", async () => {
    await mount();
    await openMenu();
    const nameSpan = [...document.querySelectorAll(".fleet-switcher-name")].find((el) =>
      (el.textContent || "").includes("orion"),
    )!;
    act(() => nameSpan.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
    await flush();
    clickEl(itemByText(rowMenu(), "Move to group…")!);
    await flush();
    clickEl(itemByText(rowMenu(), "New group…")!);
    await flush();
    const input = rowMenu()!.querySelector('input[aria-label="New group name"]') as HTMLInputElement;
    expect(input).toBeTruthy();
    setInput(input, "QA");
    keydown(input, "Enter");
    await flush();
    expect(api.createFleetGroup).toHaveBeenCalledWith("QA");
    expect(api.setFleetAgentGroup).toHaveBeenCalledWith("n2", "g-qa");
  });

  // Walk the New group… field through each validateGroupName rejection — the server is never
  // called because all three fail the local rules; a regression in the copy or the reserved-name
  // rule would show here. Copy is verbatim from the spec's Rename rules.
  it("shows the group-name validation errors (blank, reserved, duplicate)", async () => {
    await mount();
    await openMenu();
    const nameSpan = [...document.querySelectorAll(".fleet-switcher-name")].find((el) =>
      (el.textContent || "").includes("orion"),
    )!;
    act(() => nameSpan.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
    await flush();
    clickEl(itemByText(rowMenu(), "Move to group…")!);
    await flush();
    clickEl(itemByText(rowMenu(), "New group…")!);
    await flush();
    const input = rowMenu()!.querySelector('input[aria-label="New group name"]') as HTMLInputElement;
    const errText = () => rowMenu()!.querySelector(".fleet-newgroup-err")?.textContent || "";

    setInput(input, "   "); // trims to blank
    keydown(input, "Enter");
    await flush();
    expect(errText()).toContain("Give the group a name.");

    setInput(input, "Remote"); // reserved, case-insensitive
    keydown(input, "Enter");
    await flush();
    expect(errText()).toContain('"Remote" is reserved for agents on paired machines.');

    setInput(input, "Demos"); // already a group in the roster
    keydown(input, "Enter");
    await flush();
    expect(errText()).toContain('There\'s already a group called "Demos".');

    // All three were rejected locally — nothing hit the server.
    expect(api.createFleetGroup).not.toHaveBeenCalled();
    expect(api.setFleetAgentGroup).not.toHaveBeenCalled();
  });

  it("surfaces the server's error when a group create is rejected, without an unhandled rejection", async () => {
    // A name that clears the local rules but the server still 400s (a duplicate another browser
    // just made). createAndMove must catch it and return the server's copy, not reject.
    (api.createFleetGroup as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(400, 'There\'s already a group called "QA".'),
    );
    await mount();
    await openMenu();
    const nameSpan = [...document.querySelectorAll(".fleet-switcher-name")].find((el) =>
      (el.textContent || "").includes("orion"),
    )!;
    act(() => nameSpan.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
    await flush();
    clickEl(itemByText(rowMenu(), "Move to group…")!);
    await flush();
    clickEl(itemByText(rowMenu(), "New group…")!);
    await flush();
    const input = rowMenu()!.querySelector('input[aria-label="New group name"]') as HTMLInputElement;
    setInput(input, "QA");
    keydown(input, "Enter");
    await flush();
    expect(api.createFleetGroup).toHaveBeenCalledWith("QA");
    expect(api.setFleetAgentGroup).not.toHaveBeenCalled(); // the move never ran
    // The server's message shows inline and the menu stays open (it didn't close or crash).
    expect(rowMenu()!.querySelector(".fleet-newgroup-err")?.textContent).toContain(
      'There\'s already a group called "QA".',
    );
  });

  it("closes the row menu after a group is created so re-opening starts clean", async () => {
    await mount();
    await openMenu();
    const nameSpan = [...document.querySelectorAll(".fleet-switcher-name")].find((el) =>
      (el.textContent || "").includes("orion"),
    )!;
    act(() => nameSpan.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
    await flush();
    clickEl(itemByText(rowMenu(), "Move to group…")!);
    await flush();
    clickEl(itemByText(rowMenu(), "New group…")!);
    await flush();
    const input = rowMenu()!.querySelector('input[aria-label="New group name"]') as HTMLInputElement;
    setInput(input, "QA");
    keydown(input, "Enter");
    await flush();
    expect(api.createFleetGroup).toHaveBeenCalledWith("QA");
    // The 'new' view no longer lingers with the typed name — the menu closed on success.
    expect(document.querySelector('input[aria-label="New group name"]')).toBeFalsy();
  });

  it("focuses the filter on open so the operator can start typing", async () => {
    await mount();
    await openMenu();
    // The focus is scheduled on the next animation frame (jsdom backs rAF with a ~16ms timer).
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 40));
    });
    const input = document.querySelector('input[placeholder="Find an agent"]');
    expect(document.activeElement).toBe(input);
  });

  it("falls back to the plain agent name on a hard fleet-API error", async () => {
    (api.fleet as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("boom"));
    await mount();
    await flush();
    expect(document.querySelector('[data-testid="fleet-switcher"]')).toBeFalsy(); // no dropdown
    expect(container.textContent).toContain("protoagent"); // the fallback name
  });

  it("a remote agent can be pinned but cannot be moved (disabled with the spec's reason)", async () => {
    await mount();
    await openMenu();
    // Can pin.
    expect(document.querySelector('[aria-label="Pin pulsar to top"]')).toBeTruthy();
    // Cannot move: the row menu's "Move to group…" is disabled.
    const nameSpan = [...document.querySelectorAll(".fleet-switcher-name")].find((el) =>
      (el.textContent || "").includes("pulsar"),
    )!;
    act(() => nameSpan.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
    await flush();
    const move = itemByText(rowMenu(), "Move to group…")!;
    expect(move.hasAttribute("data-disabled")).toBe(true);
  });
});

// ── This card: a hub that predates the groups API, and writes that no longer fail silently. ──
describe("FleetSwitcher — hub without the groups API (#4731)", () => {
  it("hides the star and the Pin/Move items, shows the plain list, and keeps the filter", async () => {
    localStorage.setItem("protoagent.fleet.switcherRecent", JSON.stringify(["n1"])); // nova
    (api.fleet as ReturnType<typeof vi.fn>).mockResolvedValue(rosterNoGroups());
    await mount();
    await openMenu();
    // No write controls: the trailing pin star and the right-click Pin/Move menu are gone.
    expect(document.querySelector('[aria-label^="Pin "]')).toBeFalsy();
    expect(document.querySelector('[aria-label^="Unpin "]')).toBeFalsy();
    const nameSpan = [...document.querySelectorAll(".fleet-switcher-name")].find((el) =>
      (el.textContent || "").includes("nova"),
    )!;
    act(() => nameSpan.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
    await flush();
    expect(rowMenu()).toBeFalsy(); // the row-actions menu isn't even rendered on a legacy hub
    // The plain list: every agent falls into Ungrouped + Remote — no operator-group sections.
    const t = mainMenu()!.textContent || "";
    expect(t).not.toContain("Demos");
    expect(t).not.toContain("Personal");
    expect(t).toContain("Remote");
    expect(t).toContain("nova");
    expect(t).toContain("vega"); // was group:g-demos, now Ungrouped (the group no longer exists)
    // The filter stays available, but recents are retired.
    expect(document.querySelector('input[placeholder="Find an agent"]')).toBeTruthy();
    expect(t).not.toContain("Recent");
  });
});

describe("FleetSwitcher — a rejected write never fails silently (this card)", () => {
  it("rolls back the optimistic pin and shows the error copy when a pin is rejected", async () => {
    (api.pinFleetAgent as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("405 method not allowed"));
    await mount();
    await openMenu();
    clickOnly(document.querySelector('[aria-label="Pin nova to top"]')!);
    await settle();
    expect(api.pinFleetAgent).toHaveBeenCalledWith("n1");
    // Rolled back: nova is unpinned again (its star still offers "Pin", not "Unpin").
    expect(document.querySelector('[aria-label="Pin nova to top"]')).toBeTruthy();
    expect(document.querySelector('[aria-label="Unpin nova"]')).toBeFalsy();
    // …and the operator was told, with the card's generic copy (a 405 carries no useful message).
    expect(toastText()).toContain("Couldn't save that change. Try again.");
  });

  it("surfaces the hub's own message when a move is rejected by name (a 409)", async () => {
    (api.setFleetAgentGroup as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(409, 'There\'s already a group called "Demos".'),
    );
    await mount();
    await openMenu();
    const nameSpan = [...document.querySelectorAll(".fleet-switcher-name")].find((el) =>
      (el.textContent || "").includes("nova"),
    )!;
    act(() => nameSpan.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
    await flush();
    clickEl(itemByText(rowMenu(), "Move to group…")!);
    await flush();
    clickEl(itemByText(rowMenu(), "Demos")!);
    await settle();
    expect(api.setFleetAgentGroup).toHaveBeenCalledWith("n1", "g-demos");
    expect(toastText()).toContain('There\'s already a group called "Demos".');
  });

  it("never swallows a failed fleet write in an empty catch block", () => {
    // The bug this card fixes was a silent `catch {}`. Guard the source so one can't creep back.
    expect(switcherSrc).not.toMatch(/catch\s*(\([^)]*\))?\s*\{\s*\}/);
  });
});

describe("FleetSwitcher — phone bottom sheet", () => {
  beforeEach(() => {
    mobileRef.value = true;
  });

  it("opens a bottom sheet at a fixed 85dvh with ⋯ row actions, and never scrolls the page behind", async () => {
    await mount();
    container.scrollTop = 40; // a stand-in for page scroll offset
    clickEl(document.querySelector('[data-testid="fleet-switcher"]')!);
    await flush();
    const sheet = document.querySelector(".pl-drawer--bottom") as HTMLElement;
    expect(sheet).toBeTruthy();
    expect(sheet.style.height).toBe("85dvh"); // fixed tall sheet (DS Drawer size)
    expect(document.body.textContent).toContain("Switch agent"); // sheet title
    expect(document.querySelector('[aria-label^="More for "]')).toBeTruthy(); // visible ⋯ per row
    // Opening the sheet never scrolls the page behind it.
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(document.body.style.overflow).toBe("hidden"); // background scroll-locked by the DS overlay layer
    expect(container.scrollTop).toBe(40); // the ancestor was not scrolled
    // 44px touch targets ship in the stylesheet (jsdom computes no layout).
    expect(themeCss).toContain("min-height: 44px");
  });

  it("the ⋯ button opens an actions sheet (Pin / Move to group…) that replaces the switcher sheet", async () => {
    await mount();
    clickEl(document.querySelector('[data-testid="fleet-switcher"]')!);
    await flush();
    clickEl(document.querySelector('[aria-label="More for nova"]')!);
    await flush();
    // One sheet at a time: the switcher list is gone, the actions sheet is up titled with the agent.
    expect(document.querySelector('[aria-label^="More for "]')).toBeFalsy();
    const body = document.body.textContent || "";
    expect(body).toContain("Pin to top");
    expect(body).toContain("Move to group…");
  });

  it("moves an agent to a group from the actions sheet", async () => {
    await mount();
    clickEl(document.querySelector('[data-testid="fleet-switcher"]')!);
    await flush();
    clickEl(document.querySelector('[aria-label="More for orion"]')!);
    await flush();
    const moveBtn = [...document.querySelectorAll("button")].find((b) => (b.textContent || "").includes("Move to group…"))!;
    clickEl(moveBtn);
    await flush();
    const demos = [...document.querySelectorAll(".fleet-act-item")].find((b) => (b.textContent || "").includes("Demos"))!;
    clickEl(demos);
    await flush();
    expect(api.setFleetAgentGroup).toHaveBeenCalledWith("n2", "g-demos");
  });

  it("surfaces the server's error in the new-group sheet and keeps it open when the create is rejected", async () => {
    (api.createFleetGroup as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new ApiError(400, 'There\'s already a group called "QA".'),
    );
    await mount();
    clickEl(document.querySelector('[data-testid="fleet-switcher"]')!);
    await flush();
    clickEl(document.querySelector('[aria-label="More for orion"]')!);
    await flush();
    const moveBtn = [...document.querySelectorAll("button")].find((b) => (b.textContent || "").includes("Move to group…"))!;
    clickEl(moveBtn);
    await flush();
    const newBtn = [...document.querySelectorAll("button")].find((b) => (b.textContent || "").includes("New group…"))!;
    clickEl(newBtn);
    await flush();
    const input = document.querySelector('input[aria-label="New group name"]') as HTMLInputElement;
    setInput(input, "QA");
    const createBtn = [...document.querySelectorAll("button")].find((b) => (b.textContent || "").trim() === "Create")!;
    clickEl(createBtn);
    await flush();
    expect(api.createFleetGroup).toHaveBeenCalledWith("QA");
    expect(api.setFleetAgentGroup).not.toHaveBeenCalled();
    // The sheet stays on the new-group view with the server's copy — it didn't go back or crash.
    expect(document.querySelector(".fleet-newgroup-err")?.textContent).toContain('There\'s already a group called "QA".');
    expect(document.querySelector('input[aria-label="New group name"]')).toBeTruthy();
  });

  it("a remote agent's actions sheet disables Move with the spec's reason as plain text", async () => {
    await mount();
    clickEl(document.querySelector('[data-testid="fleet-switcher"]')!);
    await flush();
    clickEl(document.querySelector('[aria-label="More for pulsar"]')!);
    await flush();
    expect(document.body.textContent).toContain("Remote agents are grouped by their machine.");
  });

  it("spells out the fleet-settings disabled reason as visible text (touch has no tooltip)", async () => {
    const data = roster();
    data.agents[0].member = true; // a member window: New agent / Fleet settings are blocked
    vi.mocked(api.fleet).mockResolvedValue(data);
    await mount();
    clickEl(document.querySelector('[data-testid="fleet-switcher"]')!);
    await flush();
    // The header icons are still disabled with the reason in their title…
    expect(document.querySelector<HTMLButtonElement>('button[aria-label="Fleet settings"]')!.disabled).toBe(true);
    // …and the reason also shows as plain text in the sheet, which a touch user can read.
    expect(document.querySelector(".fleet-sheet-note")?.textContent).toContain(
      "Fleet settings are managed from the host instance",
    );
  });
});

describe.each([false, true])("FleetSwitcher once-only header (mobile=%s)", (mobile) => {
  beforeEach(() => { mobileRef.value = mobile; });

  it.each([null, "g-demos"])("renders current pinned in group %s and every other agent once", async (group) => {
    const data = roster();
    Object.assign(data.agents[0], { pinned: true, group });
    Object.assign(data.agents.find((a) => a.id === "r1")!, { pinned: true });
    vi.mocked(api.fleet).mockResolvedValue(data);
    await mount();
    await openMenu();
    const scope = document.querySelector(mobile ? ".fleet-sheet-list" : ".fleet-switcher-menu")!;
    const names = () => [...scope.querySelectorAll(mobile ? ".fleet-prow-wrap .fleet-switcher-name-text" : ".fleet-switcher-name-text")]
      .map((el) => el.textContent);
    expect(names()).toEqual(["atlas", "pulsar", "rigel", "sol", "nova", "orion", "cass", "deneb", "vega", "lyra", "mira", "quasar"]);
    expect(scope.querySelectorAll('[aria-label="current agent"]')).toHaveLength(1);
    expect(scope.textContent).not.toContain("Recent");
    const input = document.querySelector('input[placeholder="Find an agent"]') as HTMLInputElement;
    setInput(input, "Demos");
    await flush();
    expect(names()).toEqual(group ? ["atlas", "vega", "lyra"] : ["vega", "lyra"]);
    setInput(input, "rigel");
    await flush();
    expect(names()).toEqual(["rigel"]);
    expect(scope.textContent).toContain("Pinned");
    expect(scope.textContent).not.toContain("No agents match");
  });

  it("keeps a pinned remote current agent only in the main row", async () => {
    const data = roster();
    data.agents.find((a) => a.id === "r1")!.pinned = true;
    vi.mocked(api.fleet).mockResolvedValue(data);
    vi.mocked(currentSlug).mockReturnValue("r1");
    await mount();
    await openMenu();
    const scope = document.querySelector(mobile ? ".fleet-sheet-list" : ".fleet-switcher-menu")!;
    const names = [...scope.querySelectorAll(".fleet-switcher-name-text")].map((el) => el.textContent);
    expect(names[0]).toBe("pulsar");
    expect(names.filter((name) => name === "pulsar")).toHaveLength(1);
    expect(scope.querySelectorAll('[aria-label="current agent"]')).toHaveLength(1);
    expect(names).toHaveLength(data.agents.length);
  });

  it("keeps the filter and labelled icon actions in the header and invokes each action", async () => {
    const onNewAgent = vi.fn();
    const onManageFleet = vi.fn();
    await mount({ onNewAgent, onManageFleet });
    for (const [label, callback] of [["New agent", onNewAgent], ["Fleet settings", onManageFleet]] as const) {
      await openMenu();
      const header = document.querySelector(mobile ? ".fleet-sheet .pl-drawer__head" : ".fleet-switcher-header")!;
      const button = header.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!;
      expect(button).toBeTruthy();
      expect(button.title).toBe(label);
      expect(button.disabled).toBe(false);
      expect(button.querySelector("svg")).toBeTruthy();
      expect(document.querySelectorAll(`button[aria-label="${label}"]`)).toHaveLength(1);
      const input = document.querySelector('input[placeholder="Find an agent"]')!;
      expect(input.closest(".fleet-sheet-list")).toBeNull();
      expect(document.querySelector(".pl-menu__filter .pl-input-affix__icon svg")).toBeTruthy();
      if (mobile) {
        expect(header.querySelector('[aria-label="Close"]')).toBeTruthy();
        expect(document.querySelector(".fleet-sheet-list .fleet-header-actions")).toBeNull();
      } else {
        expect(header.contains(input)).toBe(true);
        expect(themeCss).toMatch(/\.fleet-switcher-header\s*\{[^}]*padding: var\(--pl-space-1\);/);
        expect(themeCss).toMatch(/\.fleet-switcher-header\s*\{[^}]*position: sticky;[^}]*top: -4px;[^}]*margin: -4px -4px[^}]*background: var\(--pl-color-bg-raised\)/);
      }
      clickOnly(button);
      await flush();
      expect(callback).toHaveBeenCalledOnce();
      expect(document.querySelector(mobile ? ".fleet-sheet" : ".fleet-switcher-menu")).toBeNull();
    }
    expect(themeCss).toMatch(/\.fleet-sheet \.pl-menu__filter \.pl-input-affix__icon svg\s*\{[^}]*width: 16px;[^}]*height: 16px/);
  });

  it("disables both header buttons with the existing member reason", async () => {
    const data = roster();
    data.agents[0].member = true;
    vi.mocked(api.fleet).mockResolvedValue(data);
    await mount();
    await openMenu();
    for (const label of ["New agent", "Fleet settings"]) {
      const button = document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)!;
      expect(button.disabled).toBe(true);
      expect(button.title).toBe("Fleet settings are managed from the host instance");
    }
  });

  it("ignores both old recents stores and never writes them on a switch", async () => {
    localStorage.setItem("protoagent.fleet.recent", JSON.stringify({ n1: 123 }));
    localStorage.setItem("protoagent.fleet.switcherRecent", JSON.stringify(["n1"]));
    const write = vi.spyOn(Storage.prototype, "setItem");
    await mount();
    await openMenu();
    expect(document.querySelector(mobile ? ".fleet-sheet-list" : ".fleet-switcher-menu")!.textContent).not.toContain("Recent");
    const input = document.querySelector('input[placeholder="Find an agent"]') as HTMLInputElement;
    setInput(input, "nova");
    await flush();
    keydown(input, "Enter");
    await flush();
    expect(agentHref).toHaveBeenCalledWith("n1");
    expect(write.mock.calls.some(([key]) => key === "protoagent.fleet.recent" || key === "protoagent.fleet.switcherRecent")).toBe(false);
    write.mockRestore();
  });
});

it("hands focus from the filter to the first agent and back with arrow keys", async () => {
  await mount();
  await openMenu();
  const input = document.querySelector('input[placeholder="Find an agent"]') as HTMLInputElement;
  keydown(input, "ArrowDown");
  const first = mainMenu()!.querySelector('[role="menuitem"]')!;
  expect(document.activeElement).toBe(first);
  keydown(first, "ArrowUp");
  expect(document.activeElement).toBe(input);
});

// The header actions are plain buttons (aria-label/title/disabled-reason a MenuItem can't carry)
// and Radix's menu blocks Tab — so they'd be keyboard-unreachable without the header's own toolbar
// wiring. Left/Right cross the filter and the two buttons; Down enters the list; Up returns to the
// filter. This is the regression the review flagged.
it("keeps the header action buttons keyboard-reachable from the filter (Right/Left/Down/Up)", async () => {
  await mount({ onNewAgent: vi.fn(), onManageFleet: vi.fn() });
  await openMenu();
  const input = document.querySelector('input[placeholder="Find an agent"]') as HTMLInputElement;
  const newBtn = document.querySelector('button[aria-label="New agent"]') as HTMLButtonElement;
  const settingsBtn = document.querySelector('button[aria-label="Fleet settings"]') as HTMLButtonElement;
  // Right from the filter (caret at the far end) hops onto the first action, then the second.
  act(() => {
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });
  keydown(input, "ArrowRight");
  expect(document.activeElement).toBe(newBtn);
  keydown(newBtn, "ArrowRight");
  expect(document.activeElement).toBe(settingsBtn);
  // Left walks back across the toolbar: second action → first action → the filter box.
  keydown(settingsBtn, "ArrowLeft");
  expect(document.activeElement).toBe(newBtn);
  keydown(newBtn, "ArrowLeft");
  expect(document.activeElement).toBe(input);
  // Down from an action drops into the list; Up from an action returns to the filter.
  act(() => newBtn.focus());
  keydown(newBtn, "ArrowDown");
  expect((document.activeElement as HTMLElement).getAttribute("role")).toBe("menuitem");
  act(() => settingsBtn.focus());
  keydown(settingsBtn, "ArrowUp");
  expect(document.activeElement).toBe(input);
});

// Below the filter threshold there is no filter box to arrow up into, so the header would be
// unreachable when focus opens on the first row (the reviewer's exact < 10-agent case). Up from
// the first row bridges to the first action button instead.
it("reaches the header buttons by arrowing up from the first row when there is no filter", async () => {
  const mk = (id: string, name: string, over: Partial<FleetAgent> = {}): FleetAgent => ({
    id,
    name,
    port: 7800,
    pid: 1,
    running: false,
    bundle: "",
    ...over,
  });
  vi.mocked(api.fleet).mockResolvedValue({
    groups: [],
    agents: [mk("host-id", "atlas", { host: true, running: true }), mk("n1", "nova"), mk("n2", "orion")],
  });
  await mount({ onNewAgent: vi.fn(), onManageFleet: vi.fn() });
  await openMenu();
  expect(document.querySelector('input[placeholder="Find an agent"]')).toBeFalsy(); // < 10 agents: no filter
  const first = mainMenu()!.querySelector('[role="menuitem"]') as HTMLElement;
  act(() => first.focus());
  keydown(first, "ArrowUp");
  expect(document.activeElement).toBe(document.querySelector('button[aria-label="New agent"]'));
});
