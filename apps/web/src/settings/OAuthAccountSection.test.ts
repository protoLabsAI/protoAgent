// The Settings account section keys on the SAVED provider so a failed live
// switch (YAML persisted, reload failed) still surfaces the sign-in path.
import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { OAuthAccountSection } from "./OAuthAccountSection";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;
let queryClient: QueryClient;

function mount() {
  act(() => {
    root.render(h(QueryClientProvider, { client: queryClient }, h(OAuthAccountSection)));
  });
}

async function flush() {
  // Two queries resolve on separate ticks; flush a few macrotasks so both commit.
  for (let i = 0; i < 3; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

function stub(liveProvider: string, savedProvider: string) {
  vi.spyOn(api, "runtimeStatus").mockResolvedValue({ model: { provider: liveProvider } } as never);
  vi.spyOn(api, "settingsSchema").mockResolvedValue({
    groups: [
      {
        section: "Model",
        category: "Model",
        fields: [
          { key: "model.name", label: "", type: "string", section: "Model", restart: false, options: [], value: "m" },
          { key: "model.provider", label: "", type: "string", section: "Model", restart: false, options: [], value: savedProvider },
        ],
      },
    ],
  } as never);
  vi.spyOn(api, "oauthStatus").mockResolvedValue({ providers: [] });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("OAuthAccountSection", () => {
  it("renders the SAVED native provider after a failed switch, with pending copy", async () => {
    stub("openai", "anthropic-oauth"); // live=gateway, saved=Claude → the failed-switch state
    mount();
    await flush();
    expect(container.textContent).toContain("Switching to your Claude subscription");
    expect(container.textContent).toContain("completes automatically");
  });

  it("renders the live provider normally when saved matches", async () => {
    stub("openai-codex", "openai-codex");
    mount();
    await flush();
    expect(container.textContent).toContain("runs on your ChatGPT subscription");
  });

  it("renders nothing for pure gateway configs", async () => {
    stub("openai", "openai");
    mount();
    await flush();
    expect(container.querySelector("[data-testid=oauth-account-section]")).toBeNull();
  });
});
