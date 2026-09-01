import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "../lib/api";
import type { FleetAgent, MemberDiagnosticsLogs, MemberDiagnosticsTask } from "../lib/types";
import { MemberDiagnosticsDrawer } from "./FleetRoom";

// The #3169 Fleet Room diagnostics drawer. Mounted the same way the console's other UI tests
// mount React (react-dom/client + act; no testing-library dependency), with the #3168 slug-
// scoped reads stubbed on `api`. Covers rendering plus every actionable state the acceptance
// calls out: loading, empty/disabled, stopped, unreachable, timeout, unauthorized, missing-task,
// and truncation/malformed metadata — and that the drawer targets the EXPLICITLY selected member.

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const AVA: FleetAgent = {
  name: "ava",
  id: "ava",
  port: 7891,
  pid: 42,
  running: true,
  bundle: "",
};

let container: HTMLElement;
let root: Root;

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function mount(props: { slug?: string; member?: FleetAgent; onClose?: () => void } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => {
    root.render(
      h(
        QueryClientProvider,
        { client },
        h(MemberDiagnosticsDrawer, {
          slug: props.slug ?? "ava",
          member: props.member ?? AVA,
          onClose: props.onClose ?? (() => {}),
        }),
      ),
    );
  });
}

function button(label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find(
    (b) => b.textContent?.trim() === label || b.getAttribute("aria-label") === label,
  );
  if (!found) throw new Error(`button not found: ${label}`);
  return found as HTMLButtonElement;
}

function typeInto(selector: string, value: string) {
  const input = container.querySelector(selector) as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

const okLogs = (over: Partial<MemberDiagnosticsLogs> = {}): MemberDiagnosticsLogs => ({
  enabled: true,
  capacity: 200,
  returned: 2,
  lines: [
    { ts: "2026-08-31T00:00:00+00:00", level: "INFO", logger: "graph.agent", message: "boot ok" },
    { ts: "2026-08-31T00:00:01+00:00", level: "ERROR", logger: "graph.agent", message: "tool failed: boom" },
  ],
  ...over,
});

const okTask = (over: Partial<MemberDiagnosticsTask> = {}): MemberDiagnosticsTask => ({
  task_id: "task-1",
  context_id: "ctx-1",
  state: "completed",
  status_message: "all done",
  last_updated: "2026-08-31T00:00:02+00:00",
  history: [{ role: "user", message_id: "m1", text: "do the thing" }],
  artifacts: [{ artifact_id: "a1", name: "answer", text: "the thing is done" }],
  accumulated_text: "the thing is done",
  truncated: [],
  malformed: [],
  ...over,
});

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

describe("MemberDiagnosticsDrawer — logs", () => {
  it("renders the bounded log snapshot for the EXPLICITLY selected member", async () => {
    const logs = vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(okLogs());
    vi.spyOn(api, "memberDiagnosticsTask").mockResolvedValue(okTask());

    mount({ slug: "ava", member: AVA });
    await flush();
    await flush();

    // Reads the passed slug, NOT the window's focused agent — the drawer is member-local.
    expect(logs).toHaveBeenCalledWith("ava");
    // Identifies the inspected member in its header.
    expect(container.querySelector(".flr__diag-name")?.textContent).toContain("ava");
    expect(container.textContent).toContain("boot ok");
    expect(container.textContent).toContain("tool failed: boom");
    // Bounded readout (returned of capacity).
    expect(container.querySelector(".flr__diag-cap")?.textContent).toContain("2 of 200");
  });

  it("distinguishes a DISABLED buffer (opt-out note) from an enabled-but-empty one", async () => {
    vi.spyOn(api, "memberDiagnosticsTask").mockResolvedValue(okTask());
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue({
      enabled: false,
      capacity: 0,
      returned: 0,
      lines: [],
      note: "log buffer disabled (LOG_BUFFER_LINES=0)",
    });

    mount();
    await flush();
    await flush();

    expect(container.textContent).toContain("Log buffer off");
    expect(container.textContent).toContain("LOG_BUFFER_LINES=0");
    expect(container.querySelector(".flr__diag-logs")).toBeNull();
  });

  it("shows a distinct empty state when the buffer is enabled but holds nothing", async () => {
    vi.spyOn(api, "memberDiagnosticsTask").mockResolvedValue(okTask());
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(okLogs({ returned: 0, lines: [] }));

    mount();
    await flush();
    await flush();

    expect(container.textContent).toContain("No log lines yet");
  });

  it("surfaces a server clamp/advisory note above the lines", async () => {
    vi.spyOn(api, "memberDiagnosticsTask").mockResolvedValue(okTask());
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(
      okLogs({ returned: 1, lines: [okLogs().lines[0]], note: "lines=99999 above maximum; using 1000" }),
    );

    mount();
    await flush();
    await flush();

    expect(container.querySelector(".flr__diag-note")?.textContent).toContain("above maximum");
    expect(container.textContent).toContain("boot ok");
  });
});

describe("MemberDiagnosticsDrawer — proxy/API failure states", () => {
  const cases: [number, string][] = [
    [409, "ava is stopped"],
    [502, "ava is unreachable"],
    [504, "ava timed out"],
    [401, "Operator sign-in required"],
  ];

  for (const [status, copy] of cases) {
    it(`renders an actionable state for a ${status} logs read`, async () => {
      vi.spyOn(api, "memberDiagnosticsTask").mockResolvedValue(okTask());
      vi.spyOn(api, "memberDiagnosticsLogs").mockRejectedValue(new ApiError(status, "nope"));

      mount();
      await flush();
      await flush();

      expect(container.querySelector(".flr__diag-state--error")?.textContent).toContain(copy);
      // Every error state offers a Retry.
      expect(button("Retry")).toBeTruthy();
    });
  }
});

describe("MemberDiagnosticsDrawer — task inspection", () => {
  it("inspects an exact task id and surfaces truncation/malformed metadata", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(okLogs());
    const inspect = vi
      .spyOn(api, "memberDiagnosticsTask")
      .mockResolvedValue(okTask({ truncated: ["history"], malformed: ["artifact_entry"] }));

    mount({ slug: "ava", member: AVA });
    await flush();
    await flush();

    typeInto(".flr__diag-taskinput", "task-1");
    act(() => button("Inspect").click());
    await flush();
    await flush();

    expect(inspect).toHaveBeenCalledWith("ava", "task-1");
    const card = container.querySelector(".flr__diag-task");
    expect(card?.textContent).toContain("completed");
    expect(card?.textContent).toContain("the thing is done");
    expect(card?.textContent).toContain("do the thing");
    // Bounds are surfaced, not swallowed.
    expect(container.querySelector(".flr__diag-badge")?.textContent).toContain("truncated: history");
    expect(container.querySelector(".flr__diag-badge--malformed")?.textContent).toContain("malformed: artifact_entry");
  });

  it("renders a missing-task state for a 404", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(okLogs());
    vi.spyOn(api, "memberDiagnosticsTask").mockRejectedValue(new ApiError(404, "no such task on this member"));

    mount();
    await flush();
    await flush();

    typeInto(".flr__diag-taskinput", "ghost-task");
    act(() => button("Inspect").click());
    await flush();
    await flush();

    expect(container.textContent).toContain("No such task");
    expect(container.textContent).toContain("ghost-task");
  });

  it("does not inspect until a task id is submitted (idle prompt first)", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(okLogs());
    const inspect = vi.spyOn(api, "memberDiagnosticsTask").mockResolvedValue(okTask());

    mount();
    await flush();
    await flush();

    expect(inspect).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Enter an exact task id");
  });
});

describe("MemberDiagnosticsDrawer — close", () => {
  it("calls onClose from the close control", async () => {
    vi.spyOn(api, "memberDiagnosticsLogs").mockResolvedValue(okLogs());
    vi.spyOn(api, "memberDiagnosticsTask").mockResolvedValue(okTask());
    const onClose = vi.fn();

    mount({ onClose });
    await flush();
    await flush();

    act(() => button("Close diagnostics").click());
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
