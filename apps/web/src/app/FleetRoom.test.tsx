// Fleet Room member diagnostics drawer (#3169) — rendering + error-state coverage. Renders the
// exported `MemberDiagnostics` in isolation (no SSE-backed activity feed) against a mocked
// diagnostics API, and unit-tests the pure error→state mapping. Uses createRoot/act + a mocked
// `api` like the other console UI suites (ProvidersPanel.ui.test.ts) — no testing-library dep.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../lib/api";
import type { DiagnosticsLogs, DiagnosticsTask, FleetAgent } from "../lib/types";
import { MemberDiagnostics, diagnosticErrorState } from "./FleetRoom";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const AVA: FleetAgent = { name: "ava", id: "ava", port: 7891, pid: 1, running: true, bundle: "" };

const LOGS_OK: DiagnosticsLogs = {
  enabled: true,
  capacity: 2000,
  returned: 2,
  lines: ["[ava] boot ok", "[ava] ready"],
};

function taskFixture(over: Partial<DiagnosticsTask> = {}): DiagnosticsTask {
  return {
    task_id: "t-1",
    context_id: "s-1",
    state: "TASK_STATE_COMPLETED",
    status_message: "",
    last_updated: "2026-08-30T10:00:00Z",
    history: [{ role: "ROLE_USER", message_id: "m1", text: "do the thing" }],
    artifacts: [{ artifact_id: "a1", name: "answer", text: "did the thing" }],
    accumulated_text: "did the thing",
    truncated: [],
    malformed: [],
    ...over,
  };
}

let container: HTMLElement;
let root: Root;

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function mount(props: Parameters<typeof MemberDiagnostics>[0]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => {
    root.render(h(QueryClientProvider, { client }, h(MemberDiagnostics, props)));
  });
}

// React overrides the input value setter, so drive controlled inputs through the native one.
function setValue(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

const text = () => container.textContent || "";
const testid = (id: string) => container.querySelector(`[data-testid="${id}"]`);

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

describe("diagnosticErrorState — proxy + member statuses map to actionable states", () => {
  it("maps the hub proxy reachability codes", () => {
    expect(diagnosticErrorState(new ApiError(409, "x"), "logs").kind).toBe("stopped");
    expect(diagnosticErrorState(new ApiError(502, "x"), "logs").kind).toBe("unreachable");
    expect(diagnosticErrorState(new ApiError(504, "x"), "logs").kind).toBe("timeout");
  });

  it("maps the member's own operator-tier + local failure codes", () => {
    expect(diagnosticErrorState(new ApiError(401, "x"), "logs").kind).toBe("unauthorized");
    expect(diagnosticErrorState(new ApiError(503, "x"), "task").kind).toBe("unconfigured");
    // A 404 is a MISSING TASK for the inspector, but a plain not-found for logs.
    expect(diagnosticErrorState(new ApiError(404, "x"), "task").kind).toBe("missing");
    expect(diagnosticErrorState(new ApiError(404, "x"), "logs").kind).toBe("error");
  });

  it("falls back to a generic, titled state for a non-HTTP error", () => {
    const s = diagnosticErrorState(new Error("boom"), "logs");
    expect(s.kind).toBe("error");
    expect(s.title).toBeTruthy();
    expect(s.hint).toContain("boom");
  });
});

describe("MemberDiagnostics — logs snapshot", () => {
  it("identifies the inspected member and renders bounded logs", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(LOGS_OK);
    mount({ slug: "ava", name: "ava", agent: AVA, onClose: () => {} });
    await flush();
    await flush();

    expect(testid("diag-member")?.textContent).toBe("ava");
    expect(api.memberDiagnosticsLogs).toHaveBeenCalledWith("ava");
    expect(testid("diag-logs")).not.toBeNull();
    expect(text()).toContain("[ava] boot ok");
    expect(text()).toContain("2 of up to 2000");
  });

  it("renders a disabled buffer as its own state with the server note (not an error)", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue({
      enabled: false,
      capacity: 0,
      returned: 0,
      lines: [],
      note: "log buffer disabled (LOG_BUFFER_LINES=0)",
    });
    mount({ slug: "ava", name: "ava", agent: AVA, onClose: () => {} });
    await flush();
    await flush();

    expect(testid("diag-logs-disabled")).not.toBeNull();
    expect(text()).toContain("LOG_BUFFER_LINES=0");
    expect(testid("diag-logs")).toBeNull();
  });

  it("renders an enabled-but-empty buffer as empty, not broken", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue({
      enabled: true,
      capacity: 2000,
      returned: 0,
      lines: [],
    });
    mount({ slug: "ava", name: "ava", agent: AVA, onClose: () => {} });
    await flush();
    await flush();

    expect(testid("diag-logs-empty")).not.toBeNull();
  });

  it("surfaces a stopped member (409) as an actionable inline state", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockRejectedValue(new ApiError(409, "agent 'ava' is not running"));
    mount({ slug: "ava", name: "ava", agent: AVA, onClose: () => {} });
    await flush();
    await flush();

    expect(testid("diag-error-stopped")).not.toBeNull();
    expect(text()).toContain("stopped");
  });

  it("still identifies a member that has left the fleet (no live roster row)", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockRejectedValue(new ApiError(502, "unreachable"));
    mount({ slug: "gone", name: "gone", agent: undefined, onClose: () => {} });
    await flush();
    await flush();

    expect(testid("diag-member")?.textContent).toBe("gone");
    expect(text()).toContain("left the fleet");
    expect(testid("diag-error-unreachable")).not.toBeNull();
  });
});

describe("MemberDiagnostics — exact task inspection", () => {
  it("inspects a typed task id for the selected member and renders the #3168 summary", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(LOGS_OK);
    const inspect = vi.spyOn(api, "memberDiagnosticsTask").mockResolvedValue(
      taskFixture({ truncated: ["history"], malformed: ["artifact_entry"] }),
    );
    mount({ slug: "ava", name: "ava", agent: AVA, onClose: () => {} });
    await flush();

    // Idle until an id is entered.
    expect(testid("diag-task")).toBeNull();

    const input = container.querySelector<HTMLInputElement>(".flr__diag-taskinput")!;
    setValue(input, "t-1");
    await flush();
    act(() => container.querySelector<HTMLButtonElement>(".flr__diag-inspect")!.click());
    await flush();
    await flush();

    expect(inspect).toHaveBeenCalledWith("ava", "t-1");
    expect(testid("diag-task")).not.toBeNull();
    expect(text()).toContain("TASK_STATE_COMPLETED");
    expect(text()).toContain("did the thing");
    expect(text()).toContain("do the thing");
    // Truncation + malformed metadata is surfaced accurately, never hidden.
    const notes = testid("diag-task-notes");
    expect(notes).not.toBeNull();
    expect(notes?.textContent).toContain("history");
    expect(notes?.textContent).toContain("artifact_entry");
  });

  it("re-inspecting the same task id refetches an updated snapshot (no stale pin)", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(LOGS_OK);
    // A task whose state/output moves between two inspects of the SAME id: the second Inspect must
    // fetch again (staleTime:Infinity would otherwise pin the first read to the unchanged key).
    const inspect = vi
      .spyOn(api, "memberDiagnosticsTask")
      .mockResolvedValueOnce(taskFixture({ state: "TASK_STATE_WORKING", accumulated_text: "partial…" }))
      .mockResolvedValueOnce(taskFixture({ state: "TASK_STATE_COMPLETED", accumulated_text: "final answer" }));
    mount({ slug: "ava", name: "ava", agent: AVA, onClose: () => {} });
    await flush();

    const input = container.querySelector<HTMLInputElement>(".flr__diag-taskinput")!;
    const inspectBtn = () => container.querySelector<HTMLButtonElement>(".flr__diag-inspect")!;
    setValue(input, "t-1");
    await flush();
    act(() => inspectBtn().click());
    await flush();
    await flush();

    expect(text()).toContain("TASK_STATE_WORKING");
    expect(text()).toContain("partial…");

    // Same id, click Inspect again → a fresh fetch, not the cached first snapshot.
    act(() => inspectBtn().click());
    await flush();
    await flush();

    expect(inspect).toHaveBeenCalledTimes(2);
    expect(inspect).toHaveBeenNthCalledWith(2, "ava", "t-1");
    expect(text()).toContain("TASK_STATE_COMPLETED");
    expect(text()).toContain("final answer");
    expect(text()).not.toContain("partial…");
  });

  it("returning from task B to task A refetches A instead of showing A's cached snapshot", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(LOGS_OK);
    const inspect = vi
      .spyOn(api, "memberDiagnosticsTask")
      .mockResolvedValueOnce(
        taskFixture({ task_id: "task-a", state: "TASK_STATE_WORKING", accumulated_text: "a is still running" }),
      )
      .mockResolvedValueOnce(
        taskFixture({ task_id: "task-b", state: "TASK_STATE_COMPLETED", accumulated_text: "b is done" }),
      )
      .mockResolvedValueOnce(
        taskFixture({ task_id: "task-a", state: "TASK_STATE_COMPLETED", accumulated_text: "a is done now" }),
      );
    mount({ slug: "ava", name: "ava", agent: AVA, onClose: () => {} });
    await flush();

    const input = container.querySelector<HTMLInputElement>(".flr__diag-taskinput")!;
    const inspectBtn = () => container.querySelector<HTMLButtonElement>(".flr__diag-inspect")!;

    setValue(input, "task-a");
    await flush();
    act(() => inspectBtn().click());
    await flush();
    await flush();
    expect(text()).toContain("a is still running");

    setValue(input, "task-b");
    await flush();
    act(() => inspectBtn().click());
    await flush();
    await flush();
    expect(text()).toContain("b is done");

    setValue(input, "task-a");
    await flush();
    act(() => inspectBtn().click());
    await flush();
    await flush();

    expect(inspect).toHaveBeenCalledTimes(3);
    expect(inspect).toHaveBeenNthCalledWith(3, "ava", "task-a");
    expect(text()).toContain("a is done now");
    expect(text()).not.toContain("a is still running");
  });

  it("surfaces a missing task id (404) as the missing-task state", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(LOGS_OK);
    vi.spyOn(api, "memberDiagnosticsTask").mockRejectedValue(new ApiError(404, "no such task on this member"));
    mount({ slug: "ava", name: "ava", agent: AVA, onClose: () => {} });
    await flush();

    const input = container.querySelector<HTMLInputElement>(".flr__diag-taskinput")!;
    setValue(input, "nope-404");
    await flush();
    act(() => container.querySelector<HTMLButtonElement>(".flr__diag-inspect")!.click());
    await flush();
    await flush();

    expect(testid("diag-error-missing")).not.toBeNull();
    expect(text()).toContain("No such task");
  });
});
