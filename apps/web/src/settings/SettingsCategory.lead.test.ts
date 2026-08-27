import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { act, createElement as h, Suspense } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { SettingsCategoryPanel } from "./SettingsCategory";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;
let queryClient: QueryClient;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.spyOn(api, "settingsSchema").mockResolvedValue({
    groups: [{ category: "Model", section: "Routing", fields: [] }],
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("SettingsCategoryPanel lead content", () => {
  it("renders the lead as the first, default-open accordion item", async () => {
    await act(async () => {
      root.render(
        h(
          ToastProvider,
          null,
          h(
            QueryClientProvider,
            { client: queryClient },
            h(
              Suspense,
              { fallback: h("span", null, "Loading") },
              h(SettingsCategoryPanel, {
                category: "Model",
                leadTitle: "Connections",
                lead: h("div", { "data-testid": "connections-content" }, "connection cards"),
              }),
            ),
          ),
        ),
      );
      await Promise.resolve();
    });

    const triggers = [...container.querySelectorAll<HTMLButtonElement>(".pl-accordion__trigger")];
    expect(triggers.map((trigger) => trigger.textContent)).toEqual(["Connections", "Routing"]);
    expect(triggers[0].getAttribute("aria-expanded")).toBe("true");
    expect(triggers[1].getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector("[data-testid=connections-content]")).not.toBeNull();
  });
});
