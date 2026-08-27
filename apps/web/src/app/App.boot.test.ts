import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@protolabsai/ui/splash", () => ({
  Splash: () => null,
  BootGate: () => h("div", { "data-testid": "boot-gate" }),
}));

vi.mock("./AuthGate", () => ({ AuthGate: () => null }));

import { App } from "./App";

let root: Root | null = null;

afterEach(() => {
  root?.unmount();
  root = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("App boot request boundary", () => {
  it("mounts only the runtime probe while the desktop sidecar is unavailable", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => new Promise<Response>(() => {}));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);

    root.render(h(QueryClientProvider, { client: queryClient }, h(App)));

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const paths = fetchMock.mock.calls.map(([input]) => String(input));
    expect(paths).toHaveLength(1);
    expect(paths[0]).toContain("/api/runtime/status");
  });
});
