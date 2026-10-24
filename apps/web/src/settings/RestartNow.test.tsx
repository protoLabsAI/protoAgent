import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { RestartNow } from "./RestartNow";

let root: Root;
let node: HTMLDivElement;
const idle = { idle: true, busy: [], supervised: true, how_to: "", boot_id: "old" };
let ready: typeof idle;
let completed: ReturnType<typeof vi.fn<() => void>>;

beforeEach(() => {
  vi.useFakeTimers();
  window.history.replaceState({}, "", "/app/");
  ready = { ...idle };
  completed = vi.fn();
  vi.spyOn(api, "restartReadiness").mockImplementation(async () => ready);
  vi.spyOn(api, "restartForSettings").mockResolvedValue({ ok: true });
  vi.spyOn(api, "stopForSettings").mockResolvedValue({ ok: true });
  vi.spyOn(api, "startAgent").mockResolvedValue({ ok: true } as Awaited<ReturnType<typeof api.startAgent>>);
  node = document.createElement("div");
  document.body.append(node);
  root = createRoot(node);
});
afterEach(async () => {
  await act(async () => { root.unmount(); await vi.advanceTimersByTimeAsync(1000); });
  node.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});
async function render() {
  await act(async () => root.render(<QueryClientProvider client={new QueryClient()}>
    <RestartNow labels={["Self update"]} onRestarted={completed} />
  </QueryClientProvider>));
}
async function click(label: string) {
  const buttons = [...document.querySelectorAll("button")].filter((b) => b.textContent?.trim() === label);
  await act(async () => buttons[buttons.length - 1].click());
}
async function tick() { await act(async () => { await vi.advanceTimersByTimeAsync(1000); }); }

it("confirms once, restarts standalone, and waits for a new process before clearing", async () => {
  await render();
  await click("Restart now");
  expect(api.restartForSettings).not.toHaveBeenCalled();
  await click("Restart now");
  expect(api.restartForSettings).toHaveBeenCalledWith("settings: Self update");
  await tick();
  expect(completed).not.toHaveBeenCalled();
  ready = { ...idle, boot_id: "new" };
  await tick();
  expect(completed).toHaveBeenCalledOnce();
});
it("names busy work and never restarts until idle", async () => {
  ready = { ...idle, idle: false, busy: ["a chat turn is running", "2 background jobs"] } as typeof idle;
  await render();
  expect(node.textContent).toContain("2 background jobs");
  await click("Restart when idle");
  await click("Restart when idle");
  await tick();
  expect(api.restartForSettings).not.toHaveBeenCalled();
  ready = idle;
  await tick();
  expect(api.restartForSettings).toHaveBeenCalledOnce();
  ready = { ...idle, boot_id: "new" };
  await tick();
});
it("stops then starts a fleet member through the hub", async () => {
  window.history.replaceState({}, "", "/app/agent/member/");
  await render();
  await click("Restart now"); await click("Restart now");
  expect(api.stopForSettings).toHaveBeenCalledWith("member", "settings: Self update");
  expect(api.startAgent).toHaveBeenCalledWith("member");
  expect(vi.mocked(api.stopForSettings).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(api.startAgent).mock.invocationCallOrder[0]);
  expect(api.restartForSettings).not.toHaveBeenCalled();
  ready = { ...idle, boot_id: "new" }; await tick();
});
it("does not start after a failed fleet stop", async () => {
  window.history.replaceState({}, "", "/app/agent/member/");
  vi.mocked(api.stopForSettings).mockResolvedValue({ ok: false });
  await render(); await click("Restart now"); await click("Restart now");
  expect(api.startAgent).not.toHaveBeenCalled();
  expect(node.textContent).toContain("could not stop");
});
it("shows manual instructions without a restart button", async () => {
  ready = { ...idle, supervised: false, how_to: "Stop and start it by hand." };
  await render();
  expect(node.textContent).toContain(ready.how_to);
  expect(node.querySelector("button")).toBeNull();
});
it("cancels a queued restart", async () => {
  ready = { ...idle, idle: false, busy: ["a chat turn is running"] } as typeof idle;
  await render(); await click("Restart when idle"); await click("Restart when idle");
  await click("Cancel"); ready = idle; await tick();
  expect(api.restartForSettings).not.toHaveBeenCalled();
});

it("does not restart a different agent when focus changes during the idle read", async () => {
  await render(); await click("Restart now");
  vi.mocked(api.restartReadiness).mockImplementationOnce(async () => {
    window.history.replaceState({}, "", "/app/agent/other/");
    return idle;
  });
  await click("Restart now");
  expect(api.restartForSettings).not.toHaveBeenCalled();
  expect(api.stopForSettings).not.toHaveBeenCalled();
});
