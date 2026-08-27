import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { ProvidersPanel } from "./ProvidersPanel";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;
let queryClient: QueryClient;

async function mount() {
  await act(async () => {
    root.render(
      h(
        ToastProvider,
        null,
        h(QueryClientProvider, { client: queryClient }, h(ProvidersPanel)),
      ),
    );
    await Promise.resolve();
  });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const providers = {
    providers: [
      {
        id: "gateway",
        type: "openai-compat",
        label: "protoLabs studio",
        base_url: "https://api.proto-labs.ai/v1",
        display: "protoLabs studio",
        has_key: true,
        in_use_by: [],
      },
      {
        id: "openai-codex",
        type: "openai-codex",
        label: "ChatGPT / Codex",
        display: "ChatGPT / Codex",
        has_key: false,
        in_use_by: ["model.name"],
      },
    ],
  };
  queryClient.setQueryData(["providers"], providers);
  vi.spyOn(api, "providers").mockResolvedValue(providers);
  vi.spyOn(api, "oauthStatus").mockResolvedValue({
    providers: [
      {
        provider: "openai-codex",
        signed_in: true,
        source: "instance_store",
        detail: "ChatGPT account",
        hint: "",
      },
    ],
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("ProvidersPanel connection cards", () => {
  it("uses the same card and status-callout hierarchy for gateway and OAuth connections", async () => {
    await mount();

    const rows = [...container.querySelectorAll<HTMLElement>("[data-testid=provider-row]")];
    expect(rows).toHaveLength(2);
    for (const row of rows) {
      expect(row.classList.contains("provider-row")).toBe(true);
      expect(row.querySelectorAll(":scope > .pl-callout, :scope > [data-testid=oauth-account-card] .pl-callout"))
        .toHaveLength(1);
      const actions = row.querySelector(".provider-row__actions")?.textContent ?? "";
      expect(actions).toContain("Edit");
      expect(actions).toContain("Test");
    }
  });

  it("leaves the Connections heading to the owning accordion item", async () => {
    await mount();
    expect(container.querySelector("h2")).toBeNull();
  });
});
