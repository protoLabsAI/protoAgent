// #2460 — the shared OAuth account card: signed-in shows Disconnect (with the
// consequence framing), signed-out shows Sign in, and disconnect converges the
// query cache (the live graph was just unloaded server-side, #2459).
//
// jsdom + react-dom/client (no @testing-library in this repo; `.test.ts` only,
// so elements are built with createElement — the AuthGate.test.ts pattern).
import { createElement as h } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { OAuthAccountCard } from "./OAuthAccount";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;
let queryClient: QueryClient;

function mount(node: Parameters<Root["render"]>[0]) {
  act(() => {
    root.render(h(QueryClientProvider, { client: queryClient }, node));
  });
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
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

function stubStatus(signedIn: boolean) {
  vi.spyOn(api, "oauthStatus").mockResolvedValue({
    providers: [
      { provider: "openai-codex", signed_in: signedIn, source: "instance_store", detail: "ChatGPT account", hint: "Sign in to connect." },
    ],
  });
}

describe("OAuthAccountCard", () => {
  it("signed in → status detail + a Disconnect action that never claims to touch the CLI login", async () => {
    stubStatus(true);
    mount(h(OAuthAccountCard, { provider: "openai-codex" }));
    await flush();
    expect(container.textContent).toContain("Signed in — ChatGPT account");
    const disconnect = [...container.querySelectorAll("button")].find((b) => b.textContent?.includes("Disconnect"));
    expect(disconnect).toBeTruthy();
    expect(disconnect!.title).toContain("never revoked remotely");
  });

  it("signed out → the provider hint + a Sign in action", async () => {
    stubStatus(false);
    mount(h(OAuthAccountCard, { provider: "openai-codex" }));
    await flush();
    expect(container.textContent).toContain("Sign in to connect.");
    expect([...container.querySelectorAll("button")].some((b) => b.textContent?.includes("Sign in with ChatGPT"))).toBe(true);
  });

  it("disconnect calls the API and invalidates the query cache (graph was unloaded)", async () => {
    stubStatus(true);
    const disc = vi.spyOn(api, "oauthDisconnect").mockResolvedValue({
      provider: "openai-codex", removed: true, revoked: false, note: "removed", graph_unloaded: true,
    });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    mount(h(OAuthAccountCard, { provider: "openai-codex" }));
    await flush();
    const btn = [...container.querySelectorAll("button")].find((b) => b.textContent?.includes("Disconnect"))!;
    await act(async () => {
      btn.click();
      await Promise.resolve();
    });
    expect(disc).toHaveBeenCalledWith("openai-codex");
    expect(invalidate).toHaveBeenCalled();
  });
});
