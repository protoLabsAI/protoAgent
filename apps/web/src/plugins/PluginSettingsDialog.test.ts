import { ToastProvider } from "@protolabsai/ui/overlays";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
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

// settings-ux C2 (#4489) fixtures — a plugin whose pane mixes the three tab kinds: a live
// schema tab (apply: live), a save-then-apply schema tab, and a path-backed page.
const overview: SettingsGroup = {
  section: "Overview",
  category: "Plugins",
  plugin_id: "boardy",
  settings_tab: { id: "overview", label: "Overview", order: 0 },
  fields: [{ ...field, key: "boardy.greeting", label: "Greeting", section: "Overview" }],
};
const threeKindTabs: PluginSettingsTabDescriptor[] = [
  { id: "overview", label: "Overview", apply: "live" },
  { id: "automation", label: "Automation" },
  { id: "projects", label: "Projects", path: "/plugins/boardy/config/projects" },
];
// A restart-only field on a save-then-apply tab — saving it must collect into the banner.
const runtimeGroup: SettingsGroup = {
  section: "Runtime",
  category: "Plugins",
  plugin_id: "boardy",
  settings_tab: { id: "runtime", label: "Runtime", order: 0 },
  fields: [{ ...field, key: "boardy.workers", label: "Workers", type: "number", restart: true, section: "Runtime", value: "2" }],
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

const hasSaveButton = () =>
  [...document.body.querySelectorAll("button")].some((b) => b.textContent?.trim() === "Save & apply");

async function setInput(key: string, value: string) {
  const input = document.body.querySelector<HTMLInputElement>(`[data-key="${key}"] input`);
  if (!input) throw new Error(`missing input ${key}`);
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function waitForIframe() {
  for (let i = 0; i < 10 && !document.body.querySelector("iframe"); i++) await act(async () => Promise.resolve());
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

describe("one save model per plugin tab (#4489)", () => {
  // r1 — a live control and a Save button never share a tab, across all three tab kinds.
  it("never shows a live control beside a Save button (schema, apply:live, path tabs)", async () => {
    await renderPane([overview, automation], threeKindTabs);

    // Default tab = Overview (apply: live): its field is a live control; no Save & apply.
    expect(document.body.textContent).toContain("Greeting");
    expect(hasSaveButton()).toBe(false);

    // Automation (no apply:) keeps today's save-then-apply — the Save button is present.
    await click("Automation");
    expect(document.body.textContent).toContain("Coder");
    expect(hasSaveButton()).toBe(true);

    // Projects (path-backed) is live: the plugin page, and no Save button.
    await click("Projects");
    await waitForIframe();
    expect(document.body.querySelector("iframe")).toBeTruthy();
    expect(hasSaveButton()).toBe(false);
  });

  // r2 — a manifest with no apply: behaves exactly as today (save-then-apply + dirty tracking).
  it("a tab without apply: keeps save-then-apply", async () => {
    const save = vi.spyOn(api, "saveSettings").mockResolvedValue({ ok: true, messages: [], restart_required: [] });
    await renderPane([automation], [{ id: "automation", label: "Automation" }]);

    expect(hasSaveButton()).toBe(true);
    await setInput("boardy.coder", "claude");
    expect(document.body.textContent).toContain("1 unsaved change");
    await click("Save & apply");
    expect(save).toHaveBeenCalledWith({ "boardy.coder": "claude" }, "agent");
  });

  // r3 — saving a restart:true field collects it into the ONE banner; no per-field badge.
  it("collects a saved restart:true field into the one restart banner", async () => {
    const save = vi.spyOn(api, "saveSettings").mockResolvedValue({ ok: true, messages: [], restart_required: ["boardy.workers"] });
    // The save invalidates the schema query; answer the refetch with the same groups so it
    // can't error and tear the pane down before the banner is read.
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ groups: [runtimeGroup] }) })));
    await renderPane([runtimeGroup], [{ id: "runtime", label: "Runtime" }]);

    // The per-field "restart" badge is gone — the row carries no "restart" marker, and the
    // banner only appears once a restart-only change has saved.
    expect(document.body.querySelector('[data-key="boardy.workers"]')!.textContent).not.toContain("restart");
    expect(document.body.querySelector(".settings-restart-body")).toBeNull();

    await setInput("boardy.workers", "4");
    await click("Save & apply");
    await act(async () => Promise.resolve());

    expect(save).toHaveBeenCalledTimes(1);
    const banner = document.body.querySelector(".settings-restart-body");
    expect(banner).toBeTruthy();
    expect(banner!.textContent).toContain("1 change takes effect after a restart");
    expect(banner!.textContent).toContain("Workers");
  });

  // r4 — a plugin page's restart-pending frame message joins the same banner; a message from
  // another origin is ignored.
  it("folds a plugin page's restart-pending frame message into the banner, trusting only its origin", async () => {
    await renderPane([automation], tabs); // default active = Projects (path) → iframe mounts
    await waitForIframe();
    const iframe = document.body.querySelector("iframe")!;

    // A message from a DIFFERENT origin is ignored (PluginView's source+origin gate).
    await act(async () => {
      window.dispatchEvent(new MessageEvent("message", {
        data: { type: "protoagent:settings:restart-pending", keys: ["Evil"] },
        origin: "https://evil.example",
        source: iframe.contentWindow,
      }));
    });
    expect(document.body.querySelector(".settings-restart-body")).toBeNull();

    // The page's own origin (apiUrl resolves the frame to window.location.origin here) is trusted.
    await act(async () => {
      window.dispatchEvent(new MessageEvent("message", {
        data: { type: "protoagent:settings:restart-pending", keys: ["Self update"] },
        origin: window.location.origin,
        source: iframe.contentWindow,
      }));
    });
    const banner = document.body.querySelector(".settings-restart-body");
    expect(banner).toBeTruthy();
    expect(banner!.textContent).toContain("Self update");
    expect(banner!.textContent).not.toContain("Evil");
  });
});
