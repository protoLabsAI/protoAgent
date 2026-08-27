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
    expect(container.querySelector('[role="tabpanel"]')?.getAttribute("aria-label")).toBe("Runtime");
    await change("project_board.coder", "codex-fast");

    await clickLabel("Review & merge");
    expect(container.textContent).toContain("Review model");
    expect(container.querySelector('[role="tabpanel"]')?.getAttribute("aria-label")).toBe("Review & merge");
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
});
