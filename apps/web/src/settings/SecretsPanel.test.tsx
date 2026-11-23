// Settings ▸ Secrets tests current form values and refreshes connection status (#4702).

import { ToastProvider } from "@protolabsai/ui/overlays";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";
import type { SecretsStatus, SettingsField, SettingsGroup } from "../lib/types";
import { SecretsPanel, secretsTestOverrides } from "./SecretsPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("secretsTestOverrides", () => {
  it("maps dirty secrets_manager.* keys to the server's test field names", () => {
    expect(
      secretsTestOverrides({
        "secrets_manager.project_id": "proj-1",
        "secrets_manager.host": "https://eu.infisical.com",
        "secrets_manager.client_secret": "shh",
      }),
    ).toEqual({ project_id: "proj-1", host: "https://eu.infisical.com", client_secret: "shh" });
  });

  it("drops secrets_manager knobs that aren't test parameters, and foreign keys", () => {
    expect(
      secretsTestOverrides({
        "secrets_manager.enabled": true,
        "secrets_manager.refresh_seconds": 30,
        "secrets_manager.required": true,
        "secrets_manager.override_env": false,
        "routing.aux_model": "gpt-5",
      }),
    ).toEqual({});
  });

  it("resolves a credential to its last path segment (a nested key still maps)", () => {
    expect(secretsTestOverrides({ "secrets_manager.auth.client_id": "id-1" })).toEqual({ client_id: "id-1" });
  });
});

describe("api.secretsTest ▸ request body", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({ ok: true, error: "", error_kind: "", count: 0, names: [] }),
    }));
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  const bodyOf = () => JSON.parse(fetchMock.mock.calls[0][1].body as string);

  it("posts {} when there are no unsaved edits (saved-config behaviour unchanged)", async () => {
    await api.secretsTest();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/secrets/test");
    expect(bodyOf()).toEqual({});
  });

  it("posts only the non-blank fields the operator set", async () => {
    await api.secretsTest({ project_id: "proj-1", host: "", client_secret: "shh" });
    expect(bodyOf()).toEqual({ project_id: "proj-1", client_secret: "shh" });
  });

  it("never posts the redacted placeholder as a credential", async () => {
    await api.secretsTest({
      project_id: "proj-1",
      client_id: "••••••••",
      client_secret: "•••••••• (set — leave blank to keep)",
    });
    expect(bodyOf()).toEqual({ project_id: "proj-1" });
  });
});

// ── Full panel: the footer render-function reaches the status card's Test button ──

const dependentString = (key: string, label: string): SettingsField => ({
  key,
  label,
  type: "string",
  section: "Secrets manager",
  restart: false,
  options: [],
  value: "",
  scope: "agent",
  source: "agent",
  depends_on: { key: "secrets_manager.enabled" },
});

const dependentSecret = (key: string, label: string): SettingsField => ({
  key,
  label,
  type: "secret",
  section: "Secrets manager",
  restart: false,
  options: [],
  is_set: true,
  scope: "agent",
  source: "agent",
  depends_on: { key: "secrets_manager.enabled" },
});

const groups: SettingsGroup[] = [
  {
    section: "Secrets manager",
    category: "Secrets",
    fields: [
      // Enabled is truthy so the dependent fields below render (#963 visibility).
      {
        key: "secrets_manager.enabled",
        label: "Pull secrets from a manager",
        type: "bool",
        section: "Secrets manager",
        restart: false,
        options: [],
        value: true,
        scope: "agent",
        source: "agent",
      },
      dependentString("secrets_manager.host", "Server URL"),
      dependentString("secrets_manager.project_id", "Project ID"),
      dependentSecret("secrets_manager.client_id", "Machine identity client ID"),
      dependentSecret("secrets_manager.client_secret", "Machine identity client secret"),
    ],
  },
];

const status: SecretsStatus = {
  enabled: true,
  provider: "infisical",
  host: "",
  project_id: "",
  environment: "",
  path: "",
  ok: false,
  error: "",
  error_kind: "",
  fetched_at: "",
  applied: 0,
  shadowed: [],
  refresh_seconds: 0,
  vars: [],
};

describe("SecretsPanel ▸ Test connection", () => {
  let container: HTMLElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function flush() {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
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

  async function clickTest() {
    const button = [...container.querySelectorAll("button")].find((b) => b.textContent?.includes("Test connection"));
    if (!button) throw new Error("no Test connection button");
    await act(async () => {
      button.click();
      await Promise.resolve();
    });
  }

  async function mount(schema = groups, initialStatus = status) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // Seed the schema + status so the panel renders without suspending; spies keep any
    // background refetch resolving to the same fixtures.
    client.setQueryData(queryKeys.settings, { groups: schema });
    client.setQueryData(queryKeys.secretsStatus, initialStatus);
    vi.spyOn(api, "settingsSchema").mockResolvedValue({ groups: schema });
    vi.spyOn(api, "secretsStatus").mockResolvedValue(initialStatus);
    await act(async () => {
      root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(SecretsPanel))));
    });
    await flush();
    return client;
  }

  it.each([false, true])("posts current form values (saved fields: %s)", async (saved) => {
    const fetchMock = vi.fn(async (_url: unknown, _init: unknown) => ({
      ok: true,
      json: async () => ({ ok: true, error: "", error_kind: "", count: 0, names: [] }),
    }));
    vi.stubGlobal("fetch", fetchMock);
    const values: Record<string, string> = {
      "secrets_manager.project_id": "proj-xyz",
      "secrets_manager.host": "https://eu.infisical.com",
      "secrets_manager.client_id": "identity-id",
      "secrets_manager.client_secret": "shh-secret",
    };
    const schema = saved ? groups.map((g) => ({
      ...g,
      fields: g.fields.map((f) => ({
        ...f, value: f.type === "string" ? values[f.key] : f.value,
      })),
    })) : groups;
    await mount(schema);
    for (const [key, value] of Object.entries(values)) {
      if (!saved || key.includes("client_")) await change(key, value);
    }
    await clickTest();
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/secrets/test");
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({
      project_id: "proj-xyz", host: "https://eu.infisical.com",
      client_id: "identity-id", client_secret: "shh-secret",
    });
  });

  it("omits blank fields and redacted credentials from the form request", async () => {
    const fetchMock = vi.fn(async (_url: unknown, _init: unknown) => ({
      ok: true,
      json: async () => ({ ok: true, error: "", error_kind: "", count: 0, names: [] }),
    }));
    vi.stubGlobal("fetch", fetchMock);
    await mount();
    await change("secrets_manager.client_id", "••••••••");
    await change("secrets_manager.client_secret", "•••••••• (set — leave blank to keep)");
    await clickTest();
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({});
  });

  function buttonNamed(label: string) {
    const button = [...container.querySelectorAll("button")].find((b) => b.textContent?.includes(label));
    if (!button) throw new Error(`no ${label} button`);
    return button;
  }

  it("refreshes status after save and disables Sync when the manager becomes disabled", async () => {
    vi.spyOn(api, "saveSettings").mockResolvedValue({ ok: true, messages: [], restart_required: [] });
    const client = await mount(groups, { ...status, ok: true });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    vi.mocked(api.secretsStatus).mockResolvedValue({ ...status, enabled: false, ok: true });
    const toggle = container.querySelector<HTMLElement>('[data-key="secrets_manager.enabled"] input[type="checkbox"]')!;
    await act(async () => toggle.click());
    await act(async () => buttonNamed("Save & apply").click());
    await flush();
    await flush();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.secretsStatus });
    const card = container.querySelector('[data-testid="secrets-status"]')!;
    expect(card.textContent).toContain("disabled");
    expect(card.textContent).not.toContain("connected");
    expect(buttonNamed("Sync now").disabled).toBe(true);
    // The server deliberately permits a test before enabling the manager.
    expect(buttonNamed("Test connection").disabled).toBe(false);
  });

  it.each([false, true])("refetches status after a 502 Sync (status read fails: %s)", async (readFails) => {
    vi.spyOn(api, "secretsSync").mockRejectedValue(new Error("HTTP 502"));
    await mount(groups, { ...status, ok: true });
    const read = vi.mocked(api.secretsStatus);
    read.mockClear();
    if (readFails) read.mockRejectedValue(new Error("offline"));
    else read.mockResolvedValue({ ...status, error_kind: "auth", error: "Authentication failed" });
    await act(async () => buttonNamed("Sync now").click());
    await flush();
    await flush();
    expect(read).toHaveBeenCalled();
    const card = container.querySelector('[data-testid="secrets-status"]')!;
    expect(card.textContent).not.toContain("connected");
    expect(card.textContent).toContain(readFails ? "Couldn’t load" : "Authentication failed");
  });
});
