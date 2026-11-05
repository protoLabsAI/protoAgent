// Settings ▸ Secrets ▸ Test connection must probe the operator's UNSAVED field edits, not an
// empty body (#4702 item 2). Three seams are covered here: the dirty→request-field mapping
// (`secretsTestOverrides`), the wire body `api.secretsTest` posts (blanks and the redacted
// placeholder dropped, {} when nothing is typed), and the footer render-function that hands
// the live dirty values from the panel to the status card's Test button.

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

  async function mount() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // Seed the schema + status so the panel renders without suspending; spies keep any
    // background refetch resolving to the same fixtures.
    client.setQueryData(queryKeys.settings, { groups });
    client.setQueryData(queryKeys.secretsStatus, status);
    vi.spyOn(api, "settingsSchema").mockResolvedValue({ groups });
    vi.spyOn(api, "secretsStatus").mockResolvedValue(status);
    await act(async () => {
      root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(SecretsPanel))));
    });
    await flush();
  }

  it("posts the operator's unsaved field edits under the server's field names", async () => {
    const test = vi
      .spyOn(api, "secretsTest")
      .mockResolvedValue({ ok: true, error: "", error_kind: "", count: 2, names: ["A", "B"] });
    await mount();

    await change("secrets_manager.project_id", "proj-xyz");
    await change("secrets_manager.host", "https://eu.infisical.com");
    await change("secrets_manager.client_secret", "shh-secret");
    await clickTest();

    expect(test).toHaveBeenCalledWith({
      project_id: "proj-xyz",
      host: "https://eu.infisical.com",
      client_secret: "shh-secret",
    });
  });

  it("posts {} when nothing has been edited", async () => {
    const test = vi
      .spyOn(api, "secretsTest")
      .mockResolvedValue({ ok: true, error: "", error_kind: "", count: 0, names: [] });
    await mount();

    await clickTest();

    expect(test).toHaveBeenCalledWith({});
  });
});
