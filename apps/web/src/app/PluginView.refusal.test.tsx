import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PluginView } from "./PluginView";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
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

describe("<PluginView> refuses a view that points outside the agent's own server", () => {
  it.each([
    { id: "x", label: "X", path: "https://evil.example/v" },
    { id: "x", label: "X", path: "/plugins/x/view", tabs: [{ id: "t", label: "T", path: "//evil.example/t" }] },
  ])("mounts no frame and makes no request: %j", async (view) => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("ok"));
    await act(async () => {
      root.render(<PluginView view={{ ...view, key: "plugin:x:x" } as never} />);
    });
    expect(container.querySelector("iframe")).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(container.textContent).toContain("points outside this agent’s own server");
  });
});
