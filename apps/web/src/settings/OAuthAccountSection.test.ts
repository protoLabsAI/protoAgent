// Settings ▸ Model renders a card per CONNECTED native-OAuth provider (#3097), not
// just the one `model.provider` names, listed alongside the gateway/API-key
// connection. It still keys the active-default copy on the SAVED provider so a failed
// live switch (YAML persisted, reload failed) surfaces the sign-in path.
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
  // Three section queries (runtime, schema, oauth-status) plus each rendered card's
  // own status probe resolve on separate ticks; flush several macrotasks so all commit.
  for (let i = 0; i < 8; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

type OauthProvider = { provider: string; signed_in: boolean; source: string; detail: string; hint: string };

function signedIn(provider: string, detail = "credentials found"): OauthProvider {
  return { provider, signed_in: true, source: "instance_store", detail, hint: "" };
}

// `model` defaults to a bare `{ provider: liveProvider }` (the runtime-status shape the
// section reads); pass `null` for the no-model / setup-incomplete case.
function stub(
  liveProvider: string,
  savedProvider: string,
  providers: OauthProvider[] = [],
  model: Record<string, unknown> | null = { provider: liveProvider },
) {
  vi.spyOn(api, "runtimeStatus").mockResolvedValue({ model } as never);
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
  vi.spyOn(api, "oauthStatus").mockResolvedValue({ providers } as never);
}

function cards() {
  return container.querySelectorAll("[data-testid=oauth-account-card]");
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
    expect(cards()).toHaveLength(1);
  });

  it("renders the live provider normally when saved matches", async () => {
    stub("openai-codex", "openai-codex");
    mount();
    await flush();
    expect(container.textContent).toContain("runs on your ChatGPT subscription");
    expect(cards()).toHaveLength(1);
  });

  it("renders a card for EVERY signed-in provider, including the non-active one (#3097)", async () => {
    // Active default is Claude, but ChatGPT is also signed in — both must show,
    // regardless of which one `model.provider` names.
    stub("anthropic-oauth", "anthropic-oauth", [signedIn("anthropic-oauth"), signedIn("openai-codex")]);
    mount();
    await flush();
    expect(cards()).toHaveLength(2);
    expect(container.textContent).toContain("This agent runs on your Claude subscription.");
    // The non-active provider renders with its own "connected, not the default" line.
    expect(container.textContent).toContain("Your ChatGPT subscription is connected but isn't the current default.");
    expect(container.querySelector("[data-testid=gateway-connection]")).toBeNull();
  });

  it("lists a connected provider AND the gateway when the active default is the gateway", async () => {
    // model.provider is the gateway, yet Claude is separately signed in — you can
    // manage Claude here without first switching the default to it (#3097).
    stub("openai", "openai", [signedIn("anthropic-oauth")]);
    mount();
    await flush();
    expect(cards()).toHaveLength(1);
    expect(container.textContent).toContain("Your Claude subscription is connected but isn't the current default.");
    // The gateway/API-key connection is listed alongside the native card.
    expect(container.querySelector("[data-testid=gateway-connection]")).not.toBeNull();
    expect(container.textContent).toContain("model gateway");
    expect(container.querySelector(".panel-kicker")?.textContent).toBe("Connected accounts");
  });

  it("shows the gateway connection for a pure gateway config", async () => {
    stub("openai", "openai");
    mount();
    await flush();
    expect(container.querySelector("[data-testid=oauth-account-section]")).not.toBeNull();
    expect(cards()).toHaveLength(0);
    expect(container.querySelector("[data-testid=gateway-connection]")).not.toBeNull();
    // A single connection reads in the singular, as it did before this change.
    expect(container.querySelector(".panel-kicker")?.textContent).toBe("Connected account");
  });

  it("renders nothing when no provider or gateway is configured", async () => {
    stub("", "", [], null); // no live model, no saved provider → setup incomplete
    mount();
    await flush();
    expect(container.querySelector("[data-testid=oauth-account-section]")).toBeNull();
  });
});
