import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@protolabsai/ui/overlays";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queries";
import { removeKey } from "../lib/storage";
import { MissingFolderBanners } from "./MissingFolderBanner";

vi.mock("../settings/PathPicker", () => ({
  PathPicker: ({ value, onChange }: { value: string; onChange: (s: string) => void }) =>
    <input aria-label="New location" value={value} onChange={(e) => onChange(e.target.value)} />,
}));

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const folder = { name: "promptlab-plugin", path: "/old/promptlab-plugin" };
let root: Root;
let container: HTMLDivElement;
let client: QueryClient;
beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const scope of ["host", "other"]) removeKey("session", `protoagent.missingFolders.dismissed:${scope}`);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  client.clear();
  vi.restoreAllMocks();
});
function render(scope = "host", folders = [folder]) {
  act(() => root.render(<QueryClientProvider client={client}><ToastProvider>
    <MissingFolderBanners key={scope} scope={scope} folders={folders} />
  </ToastProvider></QueryClientProvider>));
}
async function click(label: string) {
  const button = [...container.querySelectorAll("button")].find((b) => b.textContent === label);
  expect(button).toBeDefined();
  await act(async () => button!.click());
}

it("offers actions and retains session dismissal across polls and remounts, scoped to the agent", async () => {
  render();
  expect(container.textContent).toContain("Work folder not found: promptlab-plugin (/old/promptlab-plugin)");
  expect(container.textContent).toContain("Remove from work folders");
  expect(container.textContent).toContain("Choose its new location");
  await click("Dismiss");
  render("host", [{ ...folder }]);
  expect(container.textContent).not.toContain("Work folder not found");
  render("other");
  expect(container.textContent).toContain("Work folder not found");
  render("host");
  expect(container.textContent).not.toContain("Work folder not found");
  render("host", [{ ...folder, path: "/different" }]);
  expect(container.textContent).toContain("/different");
});

it("removes a folder, reports board references, and invalidates cached views", async () => {
  vi.spyOn(api, "resolveMissingFolder").mockResolvedValue({ ok: true, board_projects: ["Plugin board"] });
  const keys = [queryKeys.runtime, ["fs-projects"], ["managed-projects"]];
  for (const key of keys) client.setQueryData(key, {});
  render();
  await click("Remove from work folders");
  expect(document.body.textContent).toContain("Plugin board");
  for (const key of keys) expect(client.getQueryState(key)?.isInvalidated).toBe(true);
});

it("keeps a failed relocation actionable and saves the chosen path on retry", async () => {
  const request = vi.spyOn(api, "resolveMissingFolder").mockRejectedValueOnce(new Error("No such folder"))
    .mockResolvedValueOnce({ ok: true, board_projects: [] });
  render();
  await click("Choose its new location");
  const input = container.querySelector("input")!;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "/new/place");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await click("Save location");
  expect(container.textContent).toContain("No such folder");
  expect(container.querySelector("input")).not.toBeNull();
  await click("Save location");
  expect(request).toHaveBeenLastCalledWith({ ...folder, action: "relocate", new_path: "/new/place" });
  expect(container.querySelector("input")).toBeNull();
  expect(container.textContent).not.toContain("No such folder");
});
