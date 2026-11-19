import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import * as push from "../lib/push";
import { PushToggle } from "./PushToggle";

vi.mock("@protolabsai/ui/overlays", () => ({ useToast: () => toast }));
const toast = vi.fn();

let root: Root;
let node: HTMLDivElement;

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("protoagent.deviceToken", "own-token");
  vi.stubGlobal("isSecureContext", true);
  node = document.createElement("div");
  document.body.append(node);
  root = createRoot(node);
});
afterEach(async () => {
  await act(async () => root.unmount());
  node.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function render() {
  await act(async () => root.render(<PushToggle />));
}
const note = () => node.querySelector('[data-testid="push-note"]')?.textContent ?? "";
const button = () => node.querySelector("button");

it("turns notifications on from the tap, then offers to turn them off", async () => {
  vi.spyOn(push, "pushState").mockResolvedValue("not enrolled");
  const enroll = vi.spyOn(push, "enrollPush").mockResolvedValue("enrolled");
  const unenroll = vi.spyOn(push, "unenrollPush").mockResolvedValue();
  await render();
  expect(button()?.textContent).toContain("Turn on notifications");
  await act(async () => button()!.click());
  expect(enroll).toHaveBeenCalledOnce();
  expect(note()).toContain("On for this device");
  expect(button()?.textContent).toContain("Turn off");
  await act(async () => button()!.click());
  expect(unenroll).toHaveBeenCalledOnce();
  expect(button()?.textContent).toContain("Turn on notifications");
});

it("shows an enrolled device as on", async () => {
  vi.spyOn(push, "pushState").mockResolvedValue("enrolled");
  await render();
  expect(note()).toContain("On for this device");
});

it.each([
  ["denied", "blocked for this console"],
  ["unsupported", "can't receive notifications"],
] as const)("explains %s without a button", async (state, text) => {
  vi.spyOn(push, "pushState").mockResolvedValue(state);
  await render();
  expect(note()).toContain(text);
  expect(button()).toBeNull();
});

it("asks an unpaired console to pair first", async () => {
  localStorage.removeItem("protoagent.deviceToken");
  vi.spyOn(push, "pushState").mockResolvedValue("not enrolled");
  await render();
  expect(note()).toContain("Pair this one");
  expect(button()).toBeNull();
});

it("points a plain-http console at the https address", async () => {
  vi.stubGlobal("isSecureContext", false);
  vi.spyOn(push, "pushState").mockResolvedValue("unsupported");
  await render();
  expect(note()).toContain("https address");
  expect(button()).toBeNull();
});

it("sends an iPhone browser tab to the Home Screen first", async () => {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)");
  vi.spyOn(push, "pushState").mockResolvedValue("unsupported");
  await render();
  expect(note()).toContain("Home Screen");
  expect(button()).toBeNull();
});

it("keeps the button and says so when the agent doesn't answer", async () => {
  vi.spyOn(push, "pushState").mockResolvedValue("not enrolled");
  vi.spyOn(push, "enrollPush").mockRejectedValue(new Error("Push enrollment: HTTP 503"));
  await render();
  await act(async () => button()!.click());
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ tone: "error" }));
  expect(button()?.textContent).toContain("Turn on notifications");
});
