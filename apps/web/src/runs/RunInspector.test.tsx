// The run inspector drawer (#4726): its loading / error / empty states, the tree it draws,
// the failure text that expands on a tap, the arrow-key tree navigation, and the poll that
// runs only while an edge is in flight.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../lib/api";
import type { RunEdgeRow, RunRecord, TelemetryTurn } from "../lib/types";
import { NO_RUN, RunInspector, RunInspectorBody } from "./RunInspector";
import { closeRunInspector, openRunInspector } from "./store";

const promptViewer = vi.hoisted(() => vi.fn());
vi.mock("../chat/PromptViewer", () => ({ openPromptViewer: promptViewer }));

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function row(p: Partial<RunEdgeRow>): RunEdgeRow {
  return {
    edge_id: 1, at: "2026-11-01T10:00:10Z", from_agent: "console", to_kind: "subagent", to_name: "research", what: "dig",
    session_id: "s1", task_id: "", outcome: "ok", error: "", duration_ms: 5000, cost_usd: 0.21, origin: "task",
    edge_uuid: "e1", parent_uuid: "", run_id: "task-1", completion: "", nudges: 0, ...p,
  };
}
const TURN: TelemetryTurn = {
  task_id: "task-1", session_id: "s1", state: "completed", success: 1, model: "claude", input_tokens: 1200, output_tokens: 300,
  total_tokens: 1500, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, cost_usd: 0.08, duration_ms: 4000,
  llm_calls: 2, tool_calls: 5, created_at: "2026-11-01T10:00:00Z", ended_at: "2026-11-01T10:03:12Z", trace_id: "abcdef1234", run_id: "task-1",
};
const RUN: RunRecord = {
  run_id: "task-1",
  resolved_from: "",
  edges: [
    row({ edge_uuid: "e1", completion: "complete", nudges: 1 }),
    row({ edge_uuid: "e2", parent_uuid: "e1", to_kind: "acp", to_name: "codex", outcome: "failed", error: "timed out after 600s", cost_usd: 0.13 }),
  ],
  turns: [TURN],
  live: [],
  langfuse_trace_url_template: "https://lf.example/project/p/traces/{trace_id}",
  tracing_enabled: true,
};
const LIVE_RUN: RunRecord = { ...RUN, live: [{ id: "l1", parent_id: "e1", run_id: "task-1", to_kind: "workflow", to_name: "review", started_at: Date.now() / 1000 }] };

let el: HTMLDivElement;
let root: Root;
let client: QueryClient;

beforeEach(() => {
  el = document.createElement("div");
  document.body.append(el);
  root = createRoot(el);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  promptViewer.mockReset();
});
afterEach(() => {
  act(() => root.unmount());
  el.remove();
  client.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
  closeRunInspector();
});

async function mount(runId = "task-1") {
  await act(async () => root.render(<QueryClientProvider client={client}><RunInspectorBody runId={runId} /></QueryClientProvider>));
  await flush();
}
// React Query hands results to observers on a 0ms timer, so settle a couple of those.
async function flush() {
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}
const q = (sel: string) => el.querySelector<HTMLElement>(sel);
const all = (sel: string) => [...el.querySelectorAll<HTMLElement>(sel)];

describe("RunInspectorBody states", () => {
  it("shows loading, then the header and the tree", async () => {
    let resolve!: (r: RunRecord) => void;
    vi.spyOn(api, "runRecord").mockReturnValue(new Promise((r) => (resolve = r)));
    await act(async () => root.render(<QueryClientProvider client={client}><RunInspectorBody runId="task-1" /></QueryClientProvider>));
    expect(q('[data-testid="run-loading"]')).not.toBeNull();
    await act(async () => resolve(RUN));
    await flush();

    const head = q('[data-testid="run-header"]')!.textContent!;
    expect(head).toContain("completed");
    expect(head).toContain("$0.42");
    expect(head).toContain("3m12s");
    expect(q('[data-testid="run-completion"]')).toBeNull(); // the root is a turn, not a checked edge
    const items = all('[role="treeitem"]');
    expect(items.map((i) => i.getAttribute("aria-label"))).toEqual(["turn console, completed", "subagent research, ok", "coder codex, failed"]);
    expect(items.map((i) => i.getAttribute("aria-level"))).toEqual(["1", "2", "3"]);
    expect(q('[data-testid="run-node-completion"]')!.textContent).toBe("complete · 1 nudge");
  });

  it("shows the error with a retry", async () => {
    const spy = vi.spyOn(api, "runRecord").mockRejectedValue(new Error("HTTP 500"));
    await mount();
    expect(q('[data-testid="run-error"]')!.textContent).toContain("Couldn't load this run. HTTP 500");
    spy.mockResolvedValue(RUN);
    await act(async () => q('[data-testid="run-error"] button')!.click());
    await flush();
    expect(q('[data-testid="run-tree"]')).not.toBeNull();
  });

  it("says there is no run for an unknown id", async () => {
    vi.spyOn(api, "runRecord").mockResolvedValue({ run_id: "nope", resolved_from: "", edges: [], turns: [], live: [] });
    await mount("nope");
    expect(q('[data-testid="run-empty"]')!.textContent).toBe(NO_RUN);
    expect(q('[data-testid="run-tree"]')).toBeNull();
  });

  it("names what the id was resolved from, and flags a subagent that came back short", async () => {
    vi.spyOn(api, "runRecord").mockResolvedValue({
      ...RUN,
      resolved_from: "bg-7",
      edges: [...RUN.edges, row({ edge_uuid: "j1", origin: "background", task_id: "bg-7", to_name: "researcher", completion: "incomplete" })],
    });
    await mount("bg-7");
    expect(q('[data-testid="run-resolved-from"]')!.textContent).toBe("Opened from job bg-7.");
    expect(q('[data-testid="run-shortfalls"]')!.textContent).toContain("One subagent came back without the full deliverable");
  });
});

describe("RunInspectorBody tree", () => {
  it("clips a failure and expands it on a tap", async () => {
    vi.spyOn(api, "runRecord").mockResolvedValue(RUN);
    await mount();
    const err = q('[data-testid="run-node-error"]')!;
    expect(err.textContent).toBe("timed out after 600s");
    expect(err.getAttribute("aria-expanded")).toBe("false");
    await act(async () => err.click());
    expect(err.getAttribute("aria-expanded")).toBe("true");
    expect(err.hasAttribute("data-expanded")).toBe(true);
  });

  it("links the root turn's trace and prompts, and shows its model and counts", async () => {
    vi.spyOn(api, "runRecord").mockResolvedValue(RUN);
    await mount();
    const detail = q('[data-testid="run-node-turn"]')!;
    expect(detail.textContent).toContain("claude · 1.2k→300 tokens · 2 llm / 5 tool calls");
    expect(q('[data-testid="run-trace-link"]')!.getAttribute("href")).toBe("https://lf.example/project/p/traces/abcdef1234");
    await act(async () => q('[data-testid="run-prompts"]')!.click());
    expect(promptViewer).toHaveBeenCalledWith("task-1", "s1");
  });

  it("walks and folds with the arrow keys", async () => {
    vi.spyOn(api, "runRecord").mockResolvedValue(RUN);
    await mount();
    const items = () => all('[role="treeitem"]');
    const key = (k: string) => act(async () => { (document.activeElement as HTMLElement).dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })); });
    expect(items().map((i) => i.tabIndex)).toEqual([0, -1, -1]); // one roving tab stop
    items()[0].focus();
    await key("ArrowDown");
    expect(document.activeElement?.getAttribute("aria-label")).toBe("subagent research, ok");
    await key("ArrowLeft"); // collapse research
    expect(items()).toHaveLength(2);
    expect(items()[1].getAttribute("aria-expanded")).toBe("false");
    await key("ArrowRight"); // expand it again
    await key("ArrowRight"); // into its first child
    expect(document.activeElement?.getAttribute("aria-label")).toBe("coder codex, failed");
    await key("ArrowLeft"); // codex is expanded (it failed) → collapses
    await key("ArrowLeft"); // → its parent
    expect(document.activeElement?.getAttribute("aria-label")).toBe("subagent research, ok");
    await key("Home");
    expect(document.activeElement?.getAttribute("aria-label")).toBe("turn console, completed");
  });

  it("shows a live edge with a live dot and polls only while one is in flight", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const spy = vi.spyOn(api, "runRecord").mockResolvedValueOnce(LIVE_RUN).mockResolvedValueOnce(LIVE_RUN).mockResolvedValue(RUN);
    await mount();
    expect(q('[data-testid="run-node-live"]')).not.toBeNull();
    expect(q('[data-testid="run-live-count"]')!.textContent).toContain("1 in flight");
    expect(spy).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(spy).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(spy).toHaveBeenCalledTimes(3); // this answer has nothing live…
    expect(q('[data-testid="run-node-live"]')).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(spy).toHaveBeenCalledTimes(3); // …so the poll stopped
  });

  it("never polls a finished run", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const spy = vi.spyOn(api, "runRecord").mockResolvedValue(RUN);
    await mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

describe("RunInspector host", () => {
  it("opens on openRunInspector and closes", async () => {
    vi.spyOn(api, "runRecord").mockResolvedValue(RUN);
    await act(async () => root.render(<QueryClientProvider client={client}><RunInspector /></QueryClientProvider>));
    expect(document.querySelector('[data-testid="run-inspector"]')).toBeNull();
    await act(async () => openRunInspector("task-1"));
    await flush();
    expect(api.runRecord).toHaveBeenCalledWith("task-1");
    expect(document.querySelector('[data-testid="run-inspector"]')).not.toBeNull();
    await act(async () => closeRunInspector());
    expect(document.querySelector('[data-testid="run-inspector"]')).toBeNull();
  });
});
