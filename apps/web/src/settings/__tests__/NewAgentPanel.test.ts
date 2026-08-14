// NewAgentPanel layout contract (#2193): Name + Create sit ABOVE the archetype section so
// a growing archetype list (every installed bundle adds a card) never pushes them
// off-screen — the card list scrolls inside its own bounded container instead. The
// preview link stays attached to the archetype section below. DOM order IS tab order
// here (no tabindex overrides), so the order assertions also pin keyboard/focus order.
//
// jsdom + react-dom/client (the console has no @testing-library; the unit harness is
// `.test.ts` only, so we build elements with React.createElement rather than JSX).
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NewAgentPanel } from "../NewAgentPanel";
import { api } from "../../lib/api";
import type { Archetype, PythonRuntimePayload } from "../../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ARCHETYPES: Archetype[] = [
  { id: "basic", label: "Basic", icon: "bot", blurb: "A plain agent", bundle: null, soul: "" },
  { id: "scout", label: "Scout", icon: "search", blurb: "Research bundle", bundle: "https://example.com/scout.git", soul: "persona" },
];

// Managed-runtime payloads for the choose-time warning (#2186 follow-on).
const runtimePayload = (over: Partial<PythonRuntimePayload["python"]>): PythonRuntimePayload => ({
  python: {
    needed: true,
    managed: true,
    managed_version: "3.12.13",
    exe: "/x/python",
    baseline_installed: true,
    baseline_current: true,
    supported: true,
    target_version: "3.12.13",
    ...over,
  },
  install: { state: "idle", pct: 0, message: "", error: null },
});

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: ARCHETYPES });
  vi.spyOn(api, "pythonRuntime").mockResolvedValue(runtimePayload({})); // provisioned by default
  vi.spyOn(api, "archetypePreview").mockResolvedValue({ id: "scout", bundle: null });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

async function mountPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(h(QueryClientProvider, { client: qc }, h(ToastProvider, null, h(NewAgentPanel, {}))));
  });
  // react-query commits the resolved archetypes on a follow-up tick (its notify batching
  // isn't a plain microtask) — wait for the panel's own data-dependent markup, bounded.
  for (let i = 0; i < 50 && !container.querySelector(".archetype-preview-link"); i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 10));
    });
  }
  if (!container.querySelector(".archetype-preview-link")) {
    throw new Error("archetypes never rendered — the mocked query did not commit");
  }
}

// a precedes b in document order (which, absent tabindex, is also tab order).
function precedes(a: Element, b: Element): boolean {
  return Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
}

function createButton(): HTMLButtonElement {
  const btn = [...container.querySelectorAll("button")].find((b) => /^Create/.test(b.textContent?.trim() ?? ""));
  if (!btn) throw new Error("no Create button rendered");
  return btn;
}

function archetypeHeading(): Element {
  const el = [...container.querySelectorAll(".fleet-section-label")].find((p) => p.textContent === "Archetype");
  if (!el) throw new Error("no Archetype section label rendered");
  return el;
}

describe("NewAgentPanel — name + create above the archetype section (#2193)", () => {
  it("renders name field (with id/URL hint) → Create button → ARCHETYPE heading, in that order", async () => {
    await mountPanel();

    const nameField = container.querySelector(".archetype-name-field");
    expect(nameField).not.toBeNull();
    // The id/URL hint travels with the name field, above the archetype section too.
    expect(nameField?.textContent).toContain("it's the agent's id and URL");

    expect(precedes(nameField!, createButton())).toBe(true);
    expect(precedes(createButton(), archetypeHeading())).toBe(true);
  });

  it("wraps the archetype card list in a bounded scroll container", async () => {
    await mountPanel();

    const scroll = container.querySelector<HTMLElement>(".archetype-card-scroll");
    expect(scroll).not.toBeNull();
    // Bounded height + its own scrollbar — overflow stays inside the list, not the page.
    expect(scroll!.style.overflowY).toBe("auto");
    expect(scroll!.style.maxHeight).not.toBe("");
    // The cards live INSIDE the container and still render as the radio card group.
    expect(scroll!.textContent).toContain("Basic");
    expect(scroll!.textContent).toContain("Scout");
    // …and the container sits inside the archetype section, below the heading.
    expect(precedes(archetypeHeading(), scroll!)).toBe(true);
  });

  it("keeps the preview link attached to the archetype section, below name + create", async () => {
    await mountPanel();

    const link = container.querySelector(".archetype-preview-link");
    expect(link).not.toBeNull();
    expect(link?.textContent).toContain("Basic"); // default pick
    expect(precedes(container.querySelector(".archetype-name-field")!, link!)).toBe(true);
    expect(precedes(createButton(), link!)).toBe(true);
    expect(precedes(archetypeHeading(), link!)).toBe(true);
  });

  it("puts the name input first in tab order — before the Create button and every card control", async () => {
    await mountPanel();

    const focusables = [
      ...container.querySelectorAll<HTMLElement>("input, button, select, textarea, a[href], [tabindex]"),
    ].filter((el) => el.tabIndex >= 0 || el.matches("input, button"));
    // The source tablist (archetype | snapshot, #2106) is the ONE thing allowed ahead of
    // the name: it decides which form you are filling in, so reaching Name first would mean
    // typing into a field that then disappears. Everything else must still come after —
    // that's the #2193 invariant, that a growing archetype list can't push name+create
    // off-screen. Asserting the exception by ROLE keeps the guard: adding some other
    // control above Name still fails this.
    const beforeName = focusables.slice(
      0,
      focusables.findIndex((el) => el.getAttribute("aria-label") === "Agent name"),
    );
    expect(beforeName.every((el) => el.getAttribute("role") === "tab")).toBe(true);
    expect(beforeName.length).toBeLessThanOrEqual(2);

    const nameInput = container.querySelector('input[aria-label="Agent name"]');
    expect(nameInput).not.toBeNull();
    expect(precedes(nameInput!, createButton())).toBe(true);
  });

  it("offers both new-agent sources, archetype first (#2106)", async () => {
    await mountPanel();
    const tabs = [...container.querySelectorAll<HTMLElement>('[role="tab"]')];
    expect(tabs.map((t) => t.textContent)).toEqual(["From an archetype", "From a snapshot"]);
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
  });
});

describe("NewAgentPanel — choose-time runtime warning (#2186 follow-on)", () => {
  const DOCSY: Archetype = {
    id: "docsy",
    label: "Docsy",
    icon: "briefcase",
    blurb: "Documents",
    bundle: "https://example.com/docsy.git",
    soul: "d",
    requires: ["python_runtime"],
  };

  async function pickDocsy() {
    const radio = [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')].find(
      (r) => r.value === "docsy",
    );
    if (!radio) throw new Error("no docsy radio card rendered");
    await act(async () => {
      radio.click();
    });
  }

  it("warns when the picked archetype requires the runtime and it isn't provisioned", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: [...ARCHETYPES, DOCSY] });
    vi.spyOn(api, "pythonRuntime").mockResolvedValue(runtimePayload({ managed: false, baseline_current: false }));
    await mountPanel();
    await pickDocsy();

    const note = container.querySelector(".archetype-runtime-notice");
    expect(note).not.toBeNull();
    expect(note!.textContent).toContain("Docsy needs the managed Python runtime");
    expect(note!.textContent).toContain("Settings ▸ Tools");
  });

  it("stays silent when the runtime is provisioned, and for archetypes without the requirement", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: [...ARCHETYPES, DOCSY] });
    await mountPanel(); // default mock: provisioned
    await pickDocsy();
    expect(container.querySelector(".archetype-runtime-notice")).toBeNull();
  });

  it("stays silent for a non-requiring archetype even when the runtime is missing", async () => {
    vi.spyOn(api, "pythonRuntime").mockResolvedValue(runtimePayload({ managed: false, baseline_current: false }));
    await mountPanel(); // default pick = basic, no requires
    expect(container.querySelector(".archetype-runtime-notice")).toBeNull();
  });
});

describe("NewAgentPanel — advanced-tier archetypes collapse behind a toggle", () => {
  // basic (standard, no tier) renders inline; pm (tier: "advanced") files under the toggle.
  const WITH_ADVANCED: Archetype[] = [
    { id: "basic", label: "Basic", icon: "bot", blurb: "A plain agent", bundle: null, soul: "" },
    {
      id: "pm",
      label: "Project Manager",
      icon: "clipboard",
      blurb: "PM bundle",
      bundle: "https://example.com/pm.git",
      soul: "pm-persona",
      tier: "advanced",
    },
  ];

  // The "Advanced (N)" disclosure button inside the advanced section.
  function advancedToggle(): HTMLButtonElement | undefined {
    return [...container.querySelectorAll<HTMLButtonElement>(".archetype-advanced button")].find((b) =>
      /^Advanced/.test(b.textContent?.trim() ?? ""),
    );
  }
  function radioFor(value: string): HTMLInputElement | undefined {
    return [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')].find((r) => r.value === value);
  }

  it("renders standard cards inline but files advanced ones under a collapsed 'Advanced (N)' toggle", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: WITH_ADVANCED });
    await mountPanel();

    // The standard card is inline and pickable from the start.
    expect(radioFor("basic")).toBeTruthy();
    // The advanced card is NOT in the DOM while the section is collapsed.
    expect(radioFor("pm")).toBeUndefined();
    // A collapsed toggle carries the advanced count + ChevronRight (collapsed) affordance.
    const toggle = advancedToggle();
    expect(toggle).toBeTruthy();
    expect(toggle!.textContent).toContain("Advanced (1)");
    expect(toggle!.getAttribute("aria-expanded")).toBe("false");
    // The advanced section sits BELOW the standard card group (DOM = visual order here).
    expect(precedes(radioFor("basic")!, toggle!)).toBe(true);
  });

  it("expands to reveal the advanced cards, then collapses them away again", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: WITH_ADVANCED });
    await mountPanel();

    await act(async () => {
      advancedToggle()!.click();
    });
    expect(advancedToggle()!.getAttribute("aria-expanded")).toBe("true");
    expect(radioFor("pm")).toBeTruthy();

    await act(async () => {
      advancedToggle()!.click();
    });
    expect(advancedToggle()!.getAttribute("aria-expanded")).toBe("false");
    expect(radioFor("pm")).toBeUndefined();
  });

  it("picking an advanced card drives the create flow identically to a standard one", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: WITH_ADVANCED });
    await mountPanel();

    await act(async () => {
      advancedToggle()!.click();
    });
    await act(async () => {
      radioFor("pm")!.click();
    });

    // Picking the bundle-backed advanced archetype flips the Create button to its bundle label
    // and the preview link to its name — same wiring the inline standard cards drive.
    expect(createButton().textContent).toContain("Create from Project Manager");
    expect(container.querySelector(".archetype-preview-link")?.textContent).toContain("Project Manager");
  });

  it("shows no advanced toggle when every archetype is standard (unchanged from before)", async () => {
    await mountPanel(); // default ARCHETYPES — both standard, no tier
    expect(advancedToggle()).toBeUndefined();
    // …and every card still renders inline.
    expect(radioFor("basic")).toBeTruthy();
    expect(radioFor("scout")).toBeTruthy();
  });
});

describe("NewAgentPanel — create forwards the archetype's capability contract (#2713)", () => {
  // The backend has persisted + checked `requires_tools` since #2315, but the panel never
  // sent it — so every console-created agent shipped without its contract (#2277's bug).
  // These tests pin the browser half of the seam.
  const WITH_CONTRACT: Archetype[] = [
    { id: "basic", label: "Basic", icon: "bot", blurb: "A plain agent", bundle: null, soul: "" },
    {
      id: "pm",
      label: "Project Manager",
      icon: "clipboard",
      blurb: "PM bundle",
      bundle: "https://example.com/pm.git",
      soul: "pm-persona",
      requires_tools: ["github_create_issue"],
    },
  ];

  function radioFor(value: string): HTMLInputElement | undefined {
    return [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')].find((r) => r.value === value);
  }

  async function typeName(name: string) {
    const input = container.querySelector<HTMLInputElement>(".archetype-name-field input");
    if (!input) throw new Error("no name input rendered");
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
    await act(async () => {
      setter.call(input, name);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  function mockCreate() {
    return vi.spyOn(api, "createAgent").mockResolvedValue({
      ok: true,
      agent: { id: "x-0", name: "x" } as never,
      installed: [],
    });
  }

  it("sends requires_tools when the picked archetype carries a contract", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: WITH_CONTRACT });
    const create = mockCreate();
    await mountPanel();

    await typeName("pm-agent");
    await act(async () => {
      radioFor("pm")!.click();
    });
    await act(async () => {
      createButton().click();
    });

    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({ requires_tools: ["github_create_issue"] }),
    );
  });

  it("omits requires_tools for a contract-less archetype", async () => {
    vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: WITH_CONTRACT });
    const create = mockCreate();
    await mountPanel();

    await typeName("plain");
    await act(async () => {
      radioFor("basic")!.click();
    });
    await act(async () => {
      createButton().click();
    });

    expect(create).toHaveBeenCalledTimes(1);
    expect(create.mock.calls[0][0].requires_tools).toBeUndefined();
  });
});
