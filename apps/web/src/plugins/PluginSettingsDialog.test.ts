import { ToastProvider } from "@protolabsai/ui/overlays";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryKeys } from "../lib/queries";
import type { PluginSettingsTabDescriptor, SettingsField, SettingsGroup } from "../lib/types";
import { PluginSettingsPane } from "./PluginSettingsDialog";

vi.mock("./settingsHydration", () => ({ pluginSchemaNeedsRefetch: () => false }));

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const field: SettingsField = {
  key: "boardy.coder",
  label: "Coder",
  type: "string",
  section: "Automation",
  restart: false,
  options: [],
  value: "codex",
  scope: "agent",
  source: "agent",
};
const automation: SettingsGroup = {
  section: "Automation",
  category: "Plugins",
  plugin_id: "boardy",
  settings_tab: { id: "automation", label: "Automation", order: 1 },
  fields: [field],
};
const tabs: PluginSettingsTabDescriptor[] = [
  { id: "projects", label: "Projects", path: "/plugins/boardy/config/projects" },
  { id: "automation", label: "Automation" },
];

// projectBoard's five tabs (projectBoard-plugin epic #617) — three plugin-owned pages plus two
// schema tabs — must all render in the pane with no manifest change (#4488 r3).
const fiveTabDescriptors: PluginSettingsTabDescriptor[] = [
  { id: "overview", label: "Overview", path: "/plugins/boardy/config/overview" },
  { id: "projects", label: "Projects", path: "/plugins/boardy/config/projects" },
  { id: "routing", label: "Routing", path: "/plugins/boardy/config/routing" },
  { id: "automation", label: "Automation" },
  { id: "maintenance", label: "Maintenance" },
];
const maintenance: SettingsGroup = {
  section: "Maintenance",
  category: "Plugins",
  plugin_id: "boardy",
  settings_tab: { id: "maintenance", label: "Maintenance", order: 5 },
  fields: [{ ...field, key: "boardy.selfupdate", label: "Self update", section: "Maintenance" }],
};

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  window.history.replaceState({}, "", "/app/agent/member/");
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200 })));
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderPane(
  groups: SettingsGroup[],
  settingsTabs: PluginSettingsTabDescriptor[],
  onBack: () => void = () => {},
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(queryKeys.settings, { groups });
  await act(async () => {
    root.render(
      h(QueryClientProvider, { client },
        h(ToastProvider, null,
          h(PluginSettingsPane, {
            pluginId: "boardy",
            pluginName: "Boardy",
            settingsTabs,
            pluginLoaded: true,
            onBack,
          }),
        ),
      ),
    );
  });
  for (let i = 0; i < 10 && !document.body.querySelector("iframe"); i++) {
    await act(async () => Promise.resolve());
  }
}

async function click(label: string) {
  const button = [...document.body.querySelectorAll("button")].find((item) => item.textContent?.trim() === label);
  if (!button) throw new Error(`missing button ${label}`);
  await act(async () => button.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

describe("plugin Configure pane", () => {
  it("renders as a full-width pane with a back link — no Dialog stacked over Settings", async () => {
    const onBack = vi.fn();
    await renderPane([automation], tabs, onBack);

    // The whole point of #4488: the Configure surface is a pane, not a <Dialog> (.pl-overlay)
    // on top of the Settings modal.
    expect(document.body.querySelector(".pl-overlay")).toBeNull();
    expect(document.body.querySelector(".plugin-settings-pane")).toBeTruthy();

    const back = document.body.querySelector<HTMLButtonElement>('[data-testid="plugin-config-back"]');
    expect(back).toBeTruthy();
    await act(async () => back!.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("Escape returns to the plugin list without closing Settings", async () => {
    const onBack = vi.fn();
    await renderPane([automation], tabs, onBack);

    // Stand-in for the Settings dialog's own Escape→close listener, registered AFTER the pane
    // mounted so the pane's window-capture listener runs first. The pane swallows the press
    // (stopImmediatePropagation), so this never fires → Settings keeps its state.
    const settingsClose = vi.fn();
    window.addEventListener("keydown", settingsClose, true);
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    });
    window.removeEventListener("keydown", settingsClose, true);

    expect(onBack).toHaveBeenCalledTimes(1);
    expect(settingsClose).not.toHaveBeenCalled();
  });

  it("Escape defers to an open dropdown in the form — pane stays, press not swallowed", async () => {
    const onBack = vi.fn();
    await renderPane([automation], tabs, onBack);

    // An open DropdownSelect/Combobox in the SettingsCategory form renders a radix popper whose
    // OWN document listener owns this Escape (#2466). The pane must not back out — and must not
    // stop the press, or the dropdown's listener never fires and it stays stuck open (#4488 r2).
    const popper = document.createElement("div");
    popper.setAttribute("data-radix-popper-content-wrapper", "");
    const listbox = document.createElement("div");
    listbox.setAttribute("role", "listbox");
    popper.appendChild(listbox);
    document.body.appendChild(popper);

    // Registered AFTER the pane's window-capture listener — in the simple case the pane swallows
    // the press and this never fires; here the pane defers, so the press reaches it (stand-in for
    // radix's own dismiss listener dispatching down the capture path).
    const radixClose = vi.fn();
    window.addEventListener("keydown", radixClose, true);
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    });
    window.removeEventListener("keydown", radixClose, true);
    popper.remove();

    expect(onBack).not.toHaveBeenCalled();
    expect(radixClose).toHaveBeenCalledTimes(1);
  });

  it("Escape defers to a dialog stacked above Settings — pane stays", async () => {
    const onBack = vi.fn();
    await renderPane([automation], tabs, onBack);

    // A DS dialog opened ON TOP of Settings is a later `.pl-overlay`; that top layer owns the
    // Escape (isTopmostOverlay), so the pane must not back out from under it (#4488 r2).
    const settings = document.createElement("div");
    settings.className = "pl-overlay";
    const inner = document.createElement("div");
    inner.className = "settings-overlay";
    settings.appendChild(inner);
    const stacked = document.createElement("div");
    stacked.className = "pl-overlay";
    document.body.appendChild(settings);
    document.body.appendChild(stacked);

    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    });
    settings.remove();
    stacked.remove();

    expect(onBack).not.toHaveBeenCalled();
  });

  it("renders projectBoard's five tabs (schema + path-backed) with no manifest change", async () => {
    await renderPane([automation, maintenance], fiveTabDescriptors);

    const labels = [...document.body.querySelectorAll("button")].map((item) => item.textContent?.trim());
    expect(labels).toEqual(
      expect.arrayContaining(["Overview", "Projects", "Routing", "Automation", "Maintenance"]),
    );
  });

  it("composes custom and schema tabs in manifest order and lazy-mounts only the active view", async () => {
    await renderPane([automation], tabs);

    expect([...document.body.querySelectorAll("button")].map((item) => item.textContent?.trim())).toEqual(
      expect.arrayContaining(["Projects", "Automation"]),
    );
    expect(document.body.querySelector("iframe")?.getAttribute("src")).toBe(
      "/agents/member/plugins/boardy/config/projects",
    );
    expect(document.body.textContent).not.toContain("Coder");

    await click("Automation");
    expect(document.body.querySelector("iframe")).toBeNull();
    expect(document.body.textContent).toContain("Coder");
    const input = document.body.querySelector<HTMLInputElement>('[data-key="boardy.coder"] input')!;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
    await act(async () => {
      setter.call(input, "claude");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });

    await click("Projects");
    await act(async () => Promise.resolve());
    expect(document.body.querySelector("iframe")).toBeTruthy();
    expect(document.body.textContent).toContain("1 unsaved change");
    await click("Automation");
    expect(document.body.querySelector<HTMLInputElement>('[data-key="boardy.coder"] input')?.value).toBe("claude");
  });

  it("renders a custom-only plugin instead of the generic empty-settings message", async () => {
    await renderPane([], [tabs[0]]);

    expect(document.body.querySelector("iframe")).toBeTruthy();
    expect(document.body.querySelector(".plugin-configure-view")).toBeTruthy();
    expect(document.body.textContent).not.toContain("Nothing to configure here.");
    expect(document.body.textContent).not.toContain("Save & apply");
  });
});
