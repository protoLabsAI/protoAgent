import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../lib/api";
import { settingsSchemaQuery } from "../lib/queries";
import type { SettingsGroup } from "../lib/types";
import { ProvidersPanel } from "./ProvidersPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
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

describe("Provider connection dialogs", () => {
  it("opens, focuses, and saves the exact connection from the account-card Edit action", async () => {
    vi.spyOn(api, "providers").mockResolvedValue({
      providers: [
        {
          id: "openai-codex",
          type: "openai-codex",
          label: "Codex team",
          display: "Codex team",
          has_key: false,
          in_use_by: ["model.name=openai-codex:gpt-5.6-sol"],
        },
      ],
    });
    vi.spyOn(api, "oauthStatus").mockResolvedValue({
      providers: [
        { provider: "openai-codex", signed_in: true, source: "instance_store", detail: "ChatGPT account", hint: "" },
      ],
    });
    const update = vi.spyOn(api, "updateProvider").mockResolvedValue({ ok: true, id: "openai-codex" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    act(() => {
      root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(ProvidersPanel))));
    });
    await flush();
    await flush();

    const edit = [...container.querySelectorAll("button")].find((button) => button.textContent?.trim() === "Edit")!;
    act(() => edit.click());

    const editor = document.querySelector('[data-testid="provider-edit-openai-codex"]')!;
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(document.body.textContent).toContain("Edit Codex team");
    const name = editor.querySelector("input") as HTMLInputElement;
    expect(name.value).toBe("Codex team");
    expect(document.querySelector('[role="dialog"]')?.contains(document.activeElement)).toBe(true);

    const save = [...document.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("Save connection"),
    )!;
    await act(async () => {
      save.click();
      await Promise.resolve();
    });
    expect(update).toHaveBeenCalledWith("openai-codex", {
      label: "Codex team",
      base_url: "",
      api_key: "",
    });
  });

  it("opens add in a focused dialog and cancels without creating a connection", async () => {
    vi.spyOn(api, "providers").mockResolvedValue({ providers: [] });
    const add = vi.spyOn(api, "addProvider").mockResolvedValue({ ok: true, id: "new-provider" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    act(() => {
      root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(ProvidersPanel))));
    });
    await flush();

    const addButton = container.querySelector('[data-testid="add-provider"]') as HTMLButtonElement;
    act(() => addButton.click());

    const editor = document.querySelector('[data-testid="provider-add-form"]')!;
    expect(editor.querySelector("input")).not.toBeNull();
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(document.body.textContent).toContain("Add a connection");
    expect(document.querySelector('[role="dialog"]')?.contains(document.activeElement)).toBe(true);

    const cancel = [...document.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === "Cancel",
    )!;
    act(() => cancel.click());

    expect(document.querySelector('[data-testid="provider-add-form"]')).toBeNull();
    expect(add).not.toHaveBeenCalled();
  });
});

// ── Resolve-references dialog (bd-v6xy) ────────────────────────────────────────
type Entry = { key: string; value: string | string[]; kind: "slot" | "favorite" | "subagent"; clearable: boolean };
type Row = {
  id: string;
  type: string;
  label: string;
  base_url?: string;
  display: string;
  has_key: boolean;
  in_use_by: string[];
  in_use: Entry[];
};

// The lanes source the dialog reuses: the settings schema's cross-provider option list.
const SCHEMA: { groups: SettingsGroup[] } = {
  groups: [
    {
      section: "Model",
      fields: [
        {
          key: "model.name",
          label: "Model",
          type: "select",
          section: "Model",
          restart: false,
          options: ["gpt-x"],
          scope: "agent",
          source: "agent",
          value: "gateway:gpt-x",
        },
        {
          key: "model.favorites",
          label: "Favorites",
          type: "string_list",
          section: "Model",
          restart: false,
          // Qualified `<pid>:<model>` cross-provider options — grouped per connection.
          options: ["gateway:gpt-x", "local-vllm:qwen3-32b"],
          scope: "agent",
          source: "agent",
          value: [],
        },
      ],
    },
  ],
};

function mountPanel(rows: Row[], client: QueryClient) {
  vi.spyOn(api, "providers").mockResolvedValue({ providers: rows });
  vi.spyOn(api, "oauthStatus").mockResolvedValue({ providers: [] });
  vi.spyOn(api, "settingsSchema").mockResolvedValue(SCHEMA);
  act(() => {
    root.render(h(QueryClientProvider, { client }, h(ToastProvider, null, h(ProvidersPanel))));
  });
}

const clickRemove = (display: string) => {
  const button = [...document.querySelectorAll("button")].find(
    (b) => b.getAttribute("aria-label") === `Remove ${display}`,
  )!;
  act(() => button.click());
};

const clickTest = (display: string) => {
  const row = [...container.querySelectorAll<HTMLElement>('[data-testid="provider-row"]')].find((providerRow) =>
    providerRow.textContent?.includes(display),
  )!;
  const button = [...row.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Test")!;
  act(() => button.click());
};

const primary = () =>
  [...document.querySelectorAll("button")].find((b) => b.textContent?.startsWith("Repoint and remove")) as
    | HTMLButtonElement
    | undefined;

const inUseRow = (inUse: Entry[], id = "gateway", display = "Gateway"): Row => ({
  id,
  type: "openai-compat",
  label: display,
  base_url: "https://api.example.com/v1",
  display,
  has_key: true,
  in_use_by: inUse.map((e) => `${e.key}=${String(e.value)}`),
  in_use: inUse,
});

describe("Resolve-references dialog (bd-v6xy)", () => {
  beforeEach(() => window.history.replaceState({}, "", "/app/")); // host window — stable query keys

  it("opens the resolve dialog (not the mutation) for a row still in use", async () => {
    const remove = vi.spyOn(api, "removeProvider");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mountPanel(
      [
        inUseRow([{ key: "routing.aux_model", value: "gateway:gpt-x", kind: "slot", clearable: true }]),
        {
          id: "spare",
          type: "openai-compat",
          label: "Spare",
          display: "Spare",
          has_key: true,
          in_use_by: [],
          in_use: [],
        },
      ],
      client,
    );
    await flush();
    await flush();

    clickRemove("Gateway");
    expect(document.querySelector('[data-testid="provider-resolve-gateway"]')).not.toBeNull();
    expect(document.body.textContent).toContain("These settings still use Gateway");
    expect(remove).not.toHaveBeenCalled();
  });

  it("removes directly (no dialog) for a row that is not in use", async () => {
    const remove = vi.spyOn(api, "removeProvider").mockResolvedValue({ ok: true, removed: "spare" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mountPanel(
      [
        inUseRow([{ key: "routing.aux_model", value: "gateway:gpt-x", kind: "slot", clearable: true }]),
        {
          id: "spare",
          type: "openai-compat",
          label: "Spare",
          display: "Spare",
          has_key: true,
          in_use_by: [],
          in_use: [],
        },
      ],
      client,
    );
    await flush();
    await flush();

    await act(async () => {
      clickRemove("Spare");
      await Promise.resolve();
    });
    expect(document.querySelector('[data-testid="provider-resolve-spare"]')).toBeNull();
    expect(remove).toHaveBeenCalledWith("spare", false);
  });

  it("shows the last-connection ConfirmDialog for an unused sole connection (unchanged)", async () => {
    const remove = vi.spyOn(api, "removeProvider");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mountPanel(
      [
        {
          id: "only",
          type: "openai-compat",
          label: "Only",
          display: "Only",
          has_key: true,
          in_use_by: [],
          in_use: [],
        },
      ],
      client,
    );
    await flush();
    await flush();

    clickRemove("Only");
    expect(document.body.textContent).toContain("Remove the last connection?");
    expect(document.querySelector('[data-testid="provider-resolve-only"]')).toBeNull();
    expect(remove).not.toHaveBeenCalled();
  });

  it("renders one row per reference: clearable defaults to Clear, model.name blocks submit, favorites are read-only", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mountPanel(
      [
        inUseRow([
          { key: "routing.aux_model", value: "gateway:gpt-x", kind: "slot", clearable: true },
          { key: "model.name", value: "gateway:gpt-x", kind: "slot", clearable: false },
          { key: "model.favorites", value: ["gateway:gpt-x", "gateway:alt"], kind: "favorite", clearable: true },
        ]),
        {
          id: "local-vllm",
          type: "openai-compat",
          label: "Local vLLM",
          display: "Local vLLM",
          has_key: true,
          in_use_by: [],
          in_use: [],
        },
      ],
      client,
    );
    await flush();
    await flush();

    clickRemove("Gateway");
    const dialog = document.querySelector('[data-testid="provider-resolve-gateway"]')!;
    expect(dialog.querySelectorAll('[data-testid^="resolve-row-"]')).toHaveLength(3);
    // The clearable slot's control shows its Clear default.
    expect(document.body.textContent).toContain("Clear (use lead model)");
    // The favorites entry is read-only prose, not a control.
    expect(document.body.textContent).toContain("2 favorites will be removed");
    // model.name has no target chosen yet → the destructive primary is disabled.
    expect(primary()?.disabled).toBe(true);
  });

  it("lets model.name be repointed only to another connection's lane and sends that exact release", async () => {
    const remove = vi.spyOn(api, "removeProvider").mockResolvedValue({ ok: true, removed: "gateway" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mountPanel(
      [
        inUseRow([{ key: "model.name", value: "gateway:gpt-x", kind: "slot", clearable: false }]),
        {
          id: "local-vllm",
          type: "openai-compat",
          label: "Local vLLM",
          display: "Local vLLM",
          has_key: true,
          in_use_by: [],
          in_use: [],
        },
      ],
      client,
    );
    await flush();
    await flush();

    clickRemove("Gateway");
    expect(primary()?.disabled).toBe(true);

    const trigger = document.querySelector('[aria-label="New target for Lead model"]') as HTMLElement;
    act(() => trigger.click());
    await flush();

    const options = [...document.querySelectorAll<HTMLElement>('[role="option"]')];
    expect(options.map((option) => option.textContent).join(" ")).toContain("qwen3-32b");
    expect(options.map((option) => option.textContent).join(" ")).not.toContain("gpt-x");
    const target = options.find((option) => option.textContent?.includes("qwen3-32b"))!;
    act(() => target.click());
    await flush();

    expect(primary()?.disabled).toBe(false);
    await act(async () => {
      primary()!.click();
      await Promise.resolve();
    });
    await flush();

    expect(remove).toHaveBeenCalledWith("gateway", false, { "model.name": "local-vllm:qwen3-32b" });
  });

  it("submits Clear/favorites as null, invalidates both caches, and closes on success", async () => {
    const remove = vi
      .spyOn(api, "removeProvider")
      .mockResolvedValue({ ok: true, removed: "gateway", released: ["routing.aux_model", "model.favorites"] });
    vi.spyOn(api, "providerModels").mockResolvedValue({ models: ["probe-only-model"], error: "" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mountPanel(
      [
        inUseRow([
          { key: "routing.aux_model", value: "gateway:gpt-x", kind: "slot", clearable: true },
          { key: "model.favorites", value: ["gateway:gpt-x"], kind: "favorite", clearable: true },
        ]),
        {
          id: "local-vllm",
          type: "openai-compat",
          label: "Local vLLM",
          display: "Local vLLM",
          has_key: true,
          in_use_by: [],
          in_use: [],
        },
      ],
      client,
    );
    await flush();
    await flush();

    clickTest("Gateway");
    await flush();
    expect(document.body.textContent).toContain("probe-only-model");

    clickRemove("Gateway");
    const invalidate = vi.spyOn(client, "invalidateQueries");
    // No non-clearable references → the primary is enabled with the Clear defaults.
    expect(primary()?.disabled).toBe(false);
    await act(async () => {
      primary()!.click();
      await Promise.resolve();
    });
    await flush();

    expect(remove).toHaveBeenCalledWith("gateway", false, {
      "routing.aux_model": null,
      "model.favorites": null,
    });
    const keys = invalidate.mock.calls.map((c) => JSON.stringify(c[0]));
    expect(keys).toContain(JSON.stringify({ queryKey: ["providers"] }));
    expect(keys).toContain(JSON.stringify({ queryKey: settingsSchemaQuery().queryKey }));
    expect(document.querySelector('[data-testid="provider-resolve-gateway"]')).toBeNull();
    expect(document.body.textContent).not.toContain("probe-only-model");
  });

  it("keeps the dialog open and shows the server detail inline on a 409/400", async () => {
    vi.spyOn(api, "removeProvider").mockRejectedValue(
      new ApiError(409, "'gateway' is still named by: model.name=gateway:gpt-x. Repoint those first."),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mountPanel(
      [
        inUseRow([{ key: "routing.aux_model", value: "gateway:gpt-x", kind: "slot", clearable: true }]),
        {
          id: "local-vllm",
          type: "openai-compat",
          label: "Local vLLM",
          display: "Local vLLM",
          has_key: true,
          in_use_by: [],
          in_use: [],
        },
      ],
      client,
    );
    await flush();
    await flush();

    clickRemove("Gateway");
    await act(async () => {
      primary()!.click();
      await Promise.resolve();
    });
    await flush();

    expect(document.querySelector('[data-testid="provider-resolve-gateway"]')).not.toBeNull();
    expect(document.body.textContent).toContain("still named by");
  });

  it("last connection + in use: opens ONLY the resolve dialog (warning inline) and sends confirm_last=true", async () => {
    const remove = vi.spyOn(api, "removeProvider").mockResolvedValue({ ok: true, removed: "only" });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    mountPanel(
      [
        {
          id: "only",
          type: "openai-compat",
          label: "Only",
          display: "Only",
          has_key: true,
          in_use_by: ["routing.aux_model=only:gpt-x"],
          in_use: [{ key: "routing.aux_model", value: "only:gpt-x", kind: "slot", clearable: true }],
        },
      ],
      client,
    );
    await flush();
    await flush();

    clickRemove("Only");
    // The resolve dialog opens; the last-connection ConfirmDialog must NOT stack with it.
    expect(document.querySelector('[data-testid="provider-resolve-only"]')).not.toBeNull();
    expect(document.body.textContent).toContain("last model connection");
    expect(document.body.textContent).not.toContain("Remove the last connection?");

    await act(async () => {
      primary()!.click();
      await Promise.resolve();
    });
    await flush();
    expect(remove).toHaveBeenCalledWith("only", true, { "routing.aux_model": null });
  });
});
