// FleetSwitcher — desktop dropdown + phone bottom sheet (docs/design/fleet-switcher-groups.md).
// Renders the REAL component against the REAL DS Menu/MenuGroup/MenuFilter/Drawer (@protolabsai/ui
// ^0.69) with a mocked `api`, in the createRoot/act + QueryClientProvider idiom the other console
// UI suites use (FleetRoom.test.tsx). The DS Menu and Drawer portal to <body>, so open content is
// queried from `document`, not the mount container.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FleetAgent, FleetStatus } from "../lib/types";
import { FleetSwitcher } from "./FleetSwitcher";
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

import { agentHref, api, ApiError } from "../lib/api";

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

let container: HTMLElement;
let root: Root;

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => {
    root.render(h(QueryClientProvider, { client }, h(FleetSwitcher, { fallbackName: "protoagent" })));
  });
  await flush();
}

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
    const order = ["Pinned", "Recent", "Demos", "Personal", "Remote", "New agent", "Fleet settings"];
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
    expect(t).not.toContain("Pinned"); // Current / Pinned / Recent hidden while filtering
    keydown(input, "Enter");
    await flush();
    expect(agentHref).toHaveBeenCalledWith("n4");
    expect(JSON.parse(localStorage.getItem("protoagent.fleet.switcherRecent") || "[]")[0]).toBe("n4");
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

  it("reads recents from storage and renders them, newest first", async () => {
    localStorage.setItem("protoagent.fleet.switcherRecent", JSON.stringify(["n2"])); // orion
    await mount();
    await openMenu();
    const t = mainMenu()!.textContent || "";
    expect(t.indexOf("Recent")).toBeGreaterThanOrEqual(0);
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
});
