import { ToastProvider } from "@protolabsai/ui/overlays";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";
import type { SettingsField, SettingsGroup } from "../lib/types";
import { SettingsCategory } from "./SettingsCategory";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const field = (key: string, label: string, value: string): SettingsField => ({
  key,
  label,
  type: "string",
  section: "Project Board",
  restart: false,
  options: [],
  value,
  scope: "agent",
  source: "agent",
});

const groups: SettingsGroup[] = [
  {
    section: "Runtime",
    category: "Plugins",
    plugin_id: "project_board",
    settings_tab: { id: "runtime", label: "Runtime", order: 0 },
    fields: [field("project_board.coder", "Coder", "codex")],
  },
  {
    section: "Review",
    category: "Plugins",
    plugin_id: "project_board",
    settings_tab: { id: "review", label: "Review & merge", order: 1 },
    fields: [field("project_board.review_model", "Review model", "claude")],
  },
];

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  window.history.replaceState({}, "", "/app/agent/member/");
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

async function clickLabel(label: string) {
  const button = [...container.querySelectorAll("button")].find((item) => item.textContent?.trim() === label);
  if (!button) throw new Error(`missing button ${label}`);
  await act(async () => button.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

async function change(key: string, value: string) {
  const input = container.querySelector<HTMLInputElement>(`[data-key="${key}"] input`);
  if (!input) throw new Error(`missing input ${key}`);
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("schema-backed plugin Configure tabs", () => {
  it("retains edits across switches and saves every tab's dirty values once", async () => {
    const save = vi.spyOn(api, "saveSettings").mockResolvedValue({ ok: true, messages: [], restart_required: [] });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(queryKeys.settings, { groups });
    await act(async () => {
      root.render(
        h(QueryClientProvider, { client },
          h(ToastProvider, null,
            h(SettingsCategory, { category: "Plugins", pluginId: "project_board", title: "Configuration" }),
          ),
        ),
      );
    });

    expect(container.textContent).toContain("Coder");
    expect(container.textContent).not.toContain("Review model");
    expect(container.querySelector('[role="tabpanel"]')?.getAttribute("aria-labelledby")).toBe(
      container.querySelector('[role="tab"][aria-selected="true"]')?.id,
    );
    await change("project_board.coder", "codex-fast");

    await clickLabel("Review & merge");
    expect(container.textContent).toContain("Review model");
    expect(container.querySelector('[role="tabpanel"]')?.getAttribute("aria-labelledby")).toBe(
      container.querySelector('[role="tab"][aria-selected="true"]')?.id,
    );
    expect(container.textContent).toContain("1 unsaved change");
    await change("project_board.review_model", "claude-opus");

    await clickLabel("Runtime");
    expect(container.querySelector<HTMLInputElement>('[data-key="project_board.coder"] input')?.value).toBe("codex-fast");
    expect(container.textContent).toContain("2 unsaved changes");

    await clickLabel("Save & apply");
    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith(
      { "project_board.coder": "codex-fast", "project_board.review_model": "claude-opus" },
      "agent",
    );
  });

  it("implements roving tab focus, arrow keys, and labelled panel relationships", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(queryKeys.settings, { groups });
    await act(async () => {
      root.render(
        h(QueryClientProvider, { client },
          h(ToastProvider, null,
            h(SettingsCategory, { category: "Plugins", pluginId: "project_board", title: "Configuration" }),
          ),
        ),
      );
    });

    const tabs = [...container.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
    const panel = container.querySelector<HTMLElement>('[role="tabpanel"]')!;
    expect(tabs.map((tab) => tab.tabIndex)).toEqual([0, -1]);
    expect(tabs[0].getAttribute("aria-controls")).toBe(panel.id);
    expect(tabs[1].getAttribute("aria-controls")).toBe(panel.id);
    expect(panel.getAttribute("aria-labelledby")).toBe(tabs[0].id);

    tabs[0].focus();
    await act(async () => tabs[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })));
    expect(document.activeElement).toBe(tabs[1]);
    expect(tabs.map((tab) => tab.tabIndex)).toEqual([-1, 0]);
    expect(panel.getAttribute("aria-labelledby")).toBe(tabs[1].id);
    expect(container.textContent).toContain("Review model");

    await act(async () => tabs[1].dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true })));
    expect(document.activeElement).toBe(tabs[0]);
    await act(async () => tabs[0].dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true })));
    expect(document.activeElement).toBe(tabs[1]);
    await act(async () => tabs[1].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true })));
    expect(document.activeElement).toBe(tabs[0]);
  });

  it("tests the full plugin section once across split tabs using current dirty values", async () => {
    const testGroups = groups.map((group) => ({
      ...group,
      test: group.settings_tab?.id === "runtime" ? { endpoint: "/api/config/test-project_board" } : undefined,
    }));
    const probe = vi.spyOn(api, "testConfig").mockResolvedValue({ ok: true, identity: "board", error: null });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(queryKeys.settings, { groups: testGroups });
    await act(async () => {
      root.render(
        h(QueryClientProvider, { client },
          h(ToastProvider, null,
            h(SettingsCategory, { category: "Plugins", pluginId: "project_board", title: "Configuration" }),
          ),
        ),
      );
    });

    await change("project_board.coder", "codex-fast");
    expect([...container.querySelectorAll("button")].filter((button) => button.textContent?.includes("Test connection"))).toHaveLength(1);
    await clickLabel("Test connection");
    expect(probe).toHaveBeenCalledTimes(1);
    expect(probe).toHaveBeenCalledWith("/api/config/test-project_board", {
      coder: "codex-fast",
      review_model: "claude",
    });
  });
});
