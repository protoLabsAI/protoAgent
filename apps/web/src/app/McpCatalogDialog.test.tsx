// DS #551 action-button rule (card 3a): the MCP catalog "All servers" back control
// is now the DS <Button variant="ghost" size="sm">, not a hand-rolled
// `<button className="mcp-catalog-back">`. Renders the REAL DS primitives so the
// swap is asserted on the actual `.pl-btn` output (overlays/forms/navigation are
// stubbed — they aren't under test and a real Dialog/Tabs pulls jsdom-hostile deps).
// Mirrors the createRoot/act + QueryClientProvider + mocked `api` pattern the other
// console UI suites use (FleetRoom.test.tsx). A `?raw` guard confirms the retired
// `.mcp-catalog-back` rule is gone from theme.css (contentDialogPadding.test.tsx idiom).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import type { McpCatalogEntry } from "../lib/types";
import { McpCatalogDialog } from "./McpCatalogDialog";
import themeCss from "./theme.css?raw";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Keep @protolabsai/ui/primitives REAL (Button/Badge) so the back control renders its
// actual `.pl-btn` classes. Stub the surrounding DS surfaces that aren't under test.
vi.mock("@protolabsai/ui/overlays", () => ({
  Dialog: ({ title, children }: { title?: ReactNode; children?: ReactNode }) =>
    h("div", { "data-testid": "mcp-dialog" }, title, children),
  useToast: () => vi.fn(),
}));
vi.mock("@protolabsai/ui/forms", () => ({
  Input: (props: Record<string, unknown>) => h("input", props),
  SecretInput: (props: Record<string, unknown>) => h("input", { ...props, type: "password" }),
}));
vi.mock("@protolabsai/ui/navigation", () => ({
  Tabs: () => h("div", { "data-testid": "mcp-tabs" }),
}));

// A catalog entry WITH inputs — picking it opens the configure step (where the back
// control lives) instead of adding in one click.
const ENTRY: McpCatalogEntry = {
  id: "acme",
  name: "Acme",
  category: "Dev",
  tagline: "Acme tools",
  template: { command: "acme", args: ["--token", "${token}"] },
  inputs: [{ key: "token", label: "API token", required: true, secret: true }],
};

let container: HTMLElement;
let root: Root;

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => {
    root.render(h(QueryClientProvider, { client }, h(McpCatalogDialog, { open: true, onClose: () => {} })));
  });
}

function buttonByText(text: string): HTMLButtonElement | undefined {
  return [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === text) as
    | HTMLButtonElement
    | undefined;
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

describe("McpCatalogDialog back control is the DS Button (#551 card 3a)", () => {
  it("renders the configure back control as a ghost/sm DS Button, not .mcp-catalog-back", async () => {
    vi.spyOn(api, "mcpCatalog").mockResolvedValue({ servers: [ENTRY] });
    mount();

    // Grid shows once the catalog query resolves; pick the input-requiring server
    // to reach the configure step where the back control lives.
    await vi.waitFor(() => expect(buttonByText("Add")).toBeDefined());
    act(() => buttonByText("Add")?.click());
    await vi.waitFor(() => expect(buttonByText("All servers")).toBeDefined());

    const back = buttonByText("All servers");
    expect(back).toBeDefined();
    // Real DS Button output: ghost variant, sm size.
    expect(back?.className).toContain("pl-btn");
    expect(back?.className).toContain("pl-btn--ghost");
    expect(back?.className).toContain("pl-btn--sm");
    // The retired hand-rolled class is gone from the markup.
    expect(back?.className).not.toContain("mcp-catalog-back");
    expect(container.querySelector(".mcp-catalog-back")).toBeNull();
  });

  it("keeps the back behavior — clicking it returns to the browse view", async () => {
    vi.spyOn(api, "mcpCatalog").mockResolvedValue({ servers: [ENTRY] });
    mount();

    await vi.waitFor(() => expect(buttonByText("Add")).toBeDefined());
    act(() => buttonByText("Add")?.click());
    await vi.waitFor(() => expect(buttonByText("All servers")).toBeDefined());
    // In the configure step the search input is gone and the selected name shows.
    expect(container.querySelector('[aria-label="search MCP servers"]')).toBeNull();
    expect(container.textContent).toContain("Acme");

    act(() => buttonByText("All servers")?.click());
    // Back on the browse grid: search input returns, no back control remains.
    await vi.waitFor(() => expect(container.querySelector('[aria-label="search MCP servers"]')).not.toBeNull());
    expect(buttonByText("All servers")).toBeUndefined();
  });

  it("removes the .mcp-catalog-back rule from theme.css", () => {
    expect(themeCss.length).toBeGreaterThan(0);
    expect(themeCss).not.toContain(".mcp-catalog-back");
  });
});
