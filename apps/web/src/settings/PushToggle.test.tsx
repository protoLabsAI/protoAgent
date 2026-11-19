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
  toast.mockClear();
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

it("a failed status read is not 'off': it offers to check again", async () => {
  const read = vi.spyOn(push, "pushState").mockRejectedValueOnce(new Error("offline")).mockResolvedValue("enrolled");
  await render();
  expect(note()).toContain("Couldn't check");
  expect(button()?.textContent).toContain("Check again");
  await act(async () => button()!.click());
  expect(read).toHaveBeenCalledTimes(2);
  expect(note()).toContain("On for this device");
});

it("re-reads permission when the operator comes back from settings", async () => {
  vi.spyOn(push, "pushState").mockResolvedValueOnce("denied").mockResolvedValue("not enrolled");
  await render();
  expect(button()).toBeNull();
  await act(async () => { window.dispatchEvent(new Event("focus")); });
  expect(button()?.textContent).toContain("Turn on notifications");
});

it("re-reads the state when turning off fails", async () => {
  vi.spyOn(push, "pushState").mockResolvedValueOnce("enrolled").mockResolvedValue("not enrolled");
  vi.spyOn(push, "unenrollPush").mockRejectedValue(new Error("Push enrollment: HTTP 503"));
  await render();
  await act(async () => button()!.click());
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ tone: "error" }));
  expect(button()?.textContent).toContain("Turn on notifications");
});

it("the permission prompt closing (a focus event) can't discard the enrollment in flight", async () => {
  const read = vi.spyOn(push, "pushState").mockResolvedValue("not enrolled");
  let granted!: (state: push.PushState) => void;
  vi.spyOn(push, "enrollPush").mockReturnValue(new Promise((resolve) => { granted = resolve; }));
  await render();
  await act(async () => button()!.click());
  await act(async () => { window.dispatchEvent(new Event("focus")); });
  expect(read).toHaveBeenCalledOnce(); // no read started mid-action
  await act(async () => granted("enrolled"));
  expect(note()).toContain("On for this device");
  expect(toast).toHaveBeenCalledWith(expect.objectContaining({ tone: "success" }));
});

it("a read that started before the tap can't overwrite what the tap decided", async () => {
  let late!: (state: push.PushState) => void;
  vi.spyOn(push, "pushState").mockResolvedValueOnce("not enrolled")
    .mockReturnValueOnce(new Promise((resolve) => { late = resolve; }));
  vi.spyOn(push, "enrollPush").mockResolvedValue("enrolled");
  await render();
  await act(async () => { window.dispatchEvent(new Event("focus")); }); // slow read begins
  await act(async () => button()!.click());
  await act(async () => late("not enrolled"));
  expect(note()).toContain("On for this device");
});
