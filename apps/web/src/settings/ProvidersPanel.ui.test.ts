import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
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
