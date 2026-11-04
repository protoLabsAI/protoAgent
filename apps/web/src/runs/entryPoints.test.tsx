// Every "View run" entry point opens the run inspector on the right id (#4726): a chat
// turn's task id, a telemetry row's run id (task id as fallback), and a background job's id.
// The org chart's is the plugin bridge, covered in app/PluginView.test.ts.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Accordion } from "@protolabsai/ui/navigation";

import { BgJobRow } from "../app/BackgroundJobs";
import { ChatMessageView } from "../chat/ChatMessageView";
import type { BackgroundJobDTO, ChatMessage } from "../lib/types";
import { RunCell } from "../telemetry/TelemetrySurface";
import { openRunInspector } from "./store";

vi.mock("./store", async (orig) => ({ ...(await orig<typeof import("./store")>()), openRunInspector: vi.fn() }));

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let el: HTMLDivElement;
let root: Root;
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });

beforeEach(() => {
  el = document.createElement("div");
  document.body.append(el);
  root = createRoot(el);
  vi.mocked(openRunInspector).mockClear();
  window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as never;
});
afterEach(() => {
  act(() => root.unmount());
  el.remove();
});

const viewRun = () => el.querySelector<HTMLButtonElement>('button[aria-label="View run"]');

describe("View run entry points", () => {
  it("a chat answer opens its turn's run, and an incognito one offers none", () => {
    const message: ChatMessage = { id: "a1", taskId: "task-7", role: "assistant", status: "done", content: "Answer" };
    act(() => root.render(h(QueryClientProvider, { client: qc }, h(ChatMessageView, { message, actions: { sessionId: "s1" } }))));
    act(() => viewRun()!.click());
    expect(openRunInspector).toHaveBeenCalledWith("task-7");

    act(() => root.render(h(QueryClientProvider, { client: qc }, h(ChatMessageView, { message, actions: { sessionId: "s1", incognito: true } }))));
    expect(viewRun()).toBeNull();
  });

  it("a telemetry row opens its run id, falling back to its task id", () => {
    act(() => root.render(h(RunCell, { turn: { run_id: "run-1", task_id: "task-1" } })));
    act(() => viewRun()!.click());
    act(() => root.render(h(RunCell, { turn: { run_id: "", task_id: "task-2" } })));
    act(() => viewRun()!.click());
    expect(vi.mocked(openRunInspector).mock.calls).toEqual([["run-1"], ["task-2"]]);
    act(() => root.render(h(RunCell, { turn: { run_id: null, task_id: "" } })));
    expect(viewRun()).toBeNull();
  });

  it("a background job row opens the run by the job's id", () => {
    const noop = () => {};
    const job: BackgroundJobDTO = { id: "bg-9", status: "completed", subagent_type: "researcher", description: "dig", result: "ok" };
    act(() => root.render(h(Accordion, null, h(BgJobRow, { job, tools: [], onStop: noop, onDelete: noop, onJumpToChat: noop }))));
    act(() => viewRun()!.click());
    expect(openRunInspector).toHaveBeenCalledWith("bg-9");
  });
});
