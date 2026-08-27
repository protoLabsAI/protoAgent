import { ToastProvider } from "@protolabsai/ui/overlays";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryKeys } from "../lib/queries";
import type { PluginSettingsTabDescriptor, SettingsField, SettingsGroup } from "../lib/types";
import { PluginSettingsDialog } from "./PluginSettingsDialog";

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

async function renderDialog(groups: SettingsGroup[], settingsTabs: PluginSettingsTabDescriptor[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(queryKeys.settings, { groups });
  await act(async () => {
    root.render(
      h(QueryClientProvider, { client },
        h(ToastProvider, null,
          h(PluginSettingsDialog, {
            pluginId: "boardy",
            pluginName: "Boardy",
            settingsTabs,
            pluginLoaded: true,
            open: true,
            onClose: () => {},
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

describe("plugin-owned Configure tabs", () => {
  it("composes custom and schema tabs in manifest order and lazy-mounts only the active view", async () => {
    await renderDialog([automation], tabs);

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
    await renderDialog([], [tabs[0]]);

    expect(document.body.querySelector("iframe")).toBeTruthy();
    expect(document.body.querySelector(".plugin-configure-view")).toBeTruthy();
    expect(document.body.textContent).not.toContain("Nothing to configure here.");
    expect(document.body.textContent).not.toContain("Save & apply");
  });
});
