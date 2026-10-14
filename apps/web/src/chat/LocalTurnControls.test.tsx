import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { LocalTurnControls, stopLocalTurns } from "./LocalTurnControls";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement;
let root: Root;
let client: QueryClient;
const turn = { id: "pending", origin: "api-chat", started_at: "2026-10-14T00:00:00Z", cancel_requested: false };

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => {
  act(() => root.unmount());
  client.clear();
  container.remove();
  vi.restoreAllMocks();
});

async function render() {
  await act(async () => {
    root.render(<QueryClientProvider client={client}><LocalTurnControls sessionId="same" visible /></QueryClientProvider>);
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
}

it("shows and stops a REST turn that has no A2A task", async () => {
  let running = true;
  vi.spyOn(api, "localChatTurns").mockImplementation(async () => ({ turns: running ? [turn] : [] }));
  const cancel = vi.spyOn(api, "cancelLocalChatTurn").mockImplementation(async () => { running = false; return { stopped: true }; });
  await render();
  expect(container.textContent).toContain("Chat request still running");
  await act(async () => { container.querySelector("button")!.click(); });
  expect(cancel).toHaveBeenCalledWith("same", "pending");
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
  expect(container.textContent).not.toContain("Chat request still running");
});

it("keeps the Stop control reachable when cleanup has not completed", async () => {
  vi.spyOn(api, "localChatTurns").mockResolvedValue({ turns: [turn] });
  vi.spyOn(api, "cancelLocalChatTurn").mockRejectedValue(new Error("Turn is still stopping"));
  await render();
  await act(async () => { container.querySelector("button")!.click(); });
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("still stopping");
  expect(container.querySelector("button")?.disabled).toBe(false);
});

it("does not finish a model-switch stop until every older request has settled", async () => {
  vi.spyOn(api, "localChatTurns").mockResolvedValue({ turns: [turn, { ...turn, id: "queued" }] });
  let finish!: (result: { stopped: boolean }) => void;
  vi.spyOn(api, "cancelLocalChatTurn").mockImplementation(async (_session, id) => id === "pending"
    ? new Promise((resolve) => { finish = resolve; }) : { stopped: true });
  let done = false;
  const pending = stopLocalTurns("same").then(() => { done = true; });
  await Promise.resolve();
  expect(done).toBe(false);
  finish({ stopped: true });
  await pending;
  expect(done).toBe(true);
});
