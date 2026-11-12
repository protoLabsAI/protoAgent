import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

const bus = vi.hoisted(() => ({ topic: () => {}, connection: (_connected: boolean) => {} }));
vi.mock("../lib/events", () => ({
  onTopic: (_topic: string, fn: () => void) => { bus.topic = fn; return () => {}; },
  onConnectionChange: (fn: (connected: boolean) => void) => { bus.connection = fn; fn(true); return () => {}; },
}));
import { api } from "../lib/api";
import { useActivityHistory } from "./useActivityHistory";
import type { WorkSource } from "../work/useWorkItems";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => vi.restoreAllMocks());

it("retains history on a failed push refresh, retries, and reconciles removed rows on reconnect", async () => {
  const row = { id: 1, state: "completed", origin: "scheduler", trigger: "job", priority: "", task_id: "", created_at: "", text: "Done", stimulus: "[Autonomous wake — scheduled run.] Check builds" };
  const read = vi.spyOn(api, "activity").mockResolvedValue({ context_id: "activity", entries: [row], messages: [] });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const host = document.createElement("div");
  const root = createRoot(host);
  let source: WorkSource;
  function Probe() { source = useActivityHistory(); return null; }
  const settle = async () => { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); }); };
  try {
    await act(async () => root.render(<QueryClientProvider client={client}><Probe /></QueryClientProvider>));
    await settle();
    expect(source!.status).toBe("ready");
    expect(source!.items[0]).toMatchObject({ title: "Check builds", raw: row });
    const loadedAt = source!.loadedAt;
    read.mockRejectedValue(new Error("offline"));
    await act(async () => bus.topic());
    await settle();
    expect(source!.status).toBe("stale");
    expect(source!.items).toHaveLength(1);
    expect(source!.loadedAt).toBe(loadedAt);
    read.mockResolvedValue({ context_id: "activity", entries: [row], messages: [] });
    await act(async () => source!.retry());
    await settle();
    expect(source!.status).toBe("ready");
    read.mockResolvedValue({ context_id: "activity", entries: [], messages: [] });
    await act(async () => { bus.connection(false); bus.connection(true); });
    await settle();
    expect(source!.items).toEqual([]);
  } finally {
    await act(async () => root.unmount());
    client.clear();
  }
});
