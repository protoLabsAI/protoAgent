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
import type { Archetype } from "../../lib/types";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ARCHETYPES: Archetype[] = [
  { id: "basic", label: "Basic", icon: "bot", blurb: "A plain agent", bundle: null, soul: "" },
  { id: "scout", label: "Scout", icon: "search", blurb: "Research bundle", bundle: "https://example.com/scout.git", soul: "persona" },
];

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.spyOn(api, "archetypes").mockResolvedValue({ archetypes: ARCHETYPES });
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
    expect(focusables[0]?.getAttribute("aria-label")).toBe("Agent name");
    const nameInput = container.querySelector('input[aria-label="Agent name"]');
    expect(nameInput).not.toBeNull();
    expect(precedes(nameInput!, createButton())).toBe(true);
  });
});
