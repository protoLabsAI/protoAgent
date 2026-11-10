import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import { LocalTurnControls, stopLocalTurns } from "./LocalTurnControls";

// The server pushes `chat.local_turns`; capture the subscriptions instead of opening a stream.
const bus = vi.hoisted(() => ({
  topics: new Map<string, Set<(data: Record<string, unknown>) => void>>(),
  connection: new Set<(connected: boolean) => void>(),
}));
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    const set = bus.topics.get(topic) ?? new Set();
    set.add(fn);
    bus.topics.set(topic, set);
    return () => set.delete(fn);
  },
  onConnectionChange: (fn: (connected: boolean) => void) => {
    bus.connection.add(fn);
    fn(true);
    return () => bus.connection.delete(fn);
  },
}));
const emit = (data: Record<string, unknown>) => bus.topics.get("chat.local_turns")?.forEach((fn) => fn(data));
const setConnected = (connected: boolean) => bus.connection.forEach((fn) => fn(connected));
const settle = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });

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

it("refreshes when the server pushes a change for this session, and never polls (#5064)", async () => {
  let running = false;
  const list = vi.spyOn(api, "localChatTurns").mockImplementation(async () => ({ turns: running ? [turn] : [] }));
  await render();
  expect(container.textContent).not.toContain("Chat request still running");
  const initial = list.mock.calls.length;
  // Idle: no request at all for well past the old one-second poll.
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1300)); });
  expect(list.mock.calls.length).toBe(initial);
  running = true;
  await act(async () => { emit({ session_id: "other" }); });
  await settle();
  expect(container.textContent).not.toContain("Chat request still running");
  await act(async () => { emit({ session_id: "same" }); });
  await settle();
  expect(container.textContent).toContain("Chat request still running");
  running = false;
  await act(async () => { emit({ session_id: "same" }); });
  await settle();
  expect(container.textContent).not.toContain("Chat request still running");
});

it("re-reads the running turns after the event stream reconnects", async () => {
  let running = false;
  vi.spyOn(api, "localChatTurns").mockImplementation(async () => ({ turns: running ? [turn] : [] }));
  await render();
  running = true; // a change the console missed while disconnected
  await act(async () => { setConnected(false); });
  await settle();
  expect(container.textContent).not.toContain("Chat request still running");
  await act(async () => { setConnected(true); });
  await settle();
  expect(container.textContent).toContain("Chat request still running");
});
