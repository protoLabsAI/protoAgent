// The Fleet Room member diagnostics drawer (#3169, over the #3168 contract). Two axes:
//   1. the pure state machine — a request error → an actionable failure kind, and a logs
//      snapshot → disabled / empty / lines;
//   2. the presentation — `DiagnosticsView` is a pure function of its props, so every state
//      (loading, each failure, logs, task summary, truncation/malformed metadata) is
//      render-testable in isolation via react-dom/server, no DOM host or query client needed.
// Plus a source guard for the invariant a render test can't see: the inspected member is
// DRAWER-LOCAL and pinned, so a fleet-selection change can never retarget it (r3).
//
// createElement (not JSX) mirrors the repo's rendering tests (app/AuthGate.test.ts): the
// vitest config runs no React plugin, so a test builds elements the same way regardless.
import { createElement as h } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import fleetRoomSource from "./FleetRoom.tsx?raw";
import {
  DiagnosticsView,
  classifyDiagnosticsError,
  diagnosticsFailureCopy,
  logsView,
  type DiagFailureKind,
} from "./FleetRoom";
import { ApiError } from "../lib/api";
import type { DiagnosticsLogs, DiagnosticsTask } from "../lib/types";

type ViewProps = Parameters<typeof DiagnosticsView>[0];

const MEMBER: ViewProps["member"] = {
  slug: "ava",
  name: "ava",
  presenceKey: "online",
  presenceLabel: "online",
};

function render(overrides: Partial<ViewProps> = {}): string {
  const props: ViewProps = {
    member: MEMBER,
    logs: { status: "loading" },
    logsFetching: false,
    onRefreshLogs: () => {},
    onClose: () => {},
    taskIdDraft: "",
    onTaskIdChange: () => {},
    onInspectTask: () => {},
    task: null,
    ...overrides,
  };
  return renderToStaticMarkup(h(DiagnosticsView, props));
}

describe("diagnostics failure classification", () => {
  it("maps every proxy + route status onto an actionable kind", () => {
    // Proxy owns member liveness (ADR 0042); the #3168 route owns its own local failures.
    expect(classifyDiagnosticsError(new ApiError(409, "not running"))).toBe("stopped");
    expect(classifyDiagnosticsError(new ApiError(502, "bad gateway"))).toBe("unreachable");
    expect(classifyDiagnosticsError(new ApiError(504, "timeout"))).toBe("timeout");
    expect(classifyDiagnosticsError(new ApiError(401, "unauthorized"))).toBe("unauthorized");
    expect(classifyDiagnosticsError(new ApiError(404, "no such task"))).toBe("missing-task");
    expect(classifyDiagnosticsError(new ApiError(503, "no store"))).toBe("disabled");
    expect(classifyDiagnosticsError(new ApiError(500, "boom"))).toBe("error");
  });

  it("treats a fetch that threw before any response as unreachable", () => {
    // WKWebView surfaces an unreachable host as `TypeError: Load failed` — nothing was hit.
    expect(classifyDiagnosticsError(new TypeError("Load failed"))).toBe("unreachable");
  });

  it("names the member in the copy and only leaks the server detail for the catch-all", () => {
    expect(diagnosticsFailureCopy("stopped", "roxy").title).toContain("roxy");
    expect(diagnosticsFailureCopy("unauthorized", "remy").hint).not.toContain("undefined");
    expect(diagnosticsFailureCopy("error", "ava", "raw server detail").hint).toBe("raw server detail");
    // a named kind carries fixed copy and ignores the detail
    expect(diagnosticsFailureCopy("stopped", "ava", "raw server detail").hint).not.toContain("raw server");
  });
});

describe("logs snapshot view state", () => {
  it("distinguishes a deliberately-disabled buffer from a working-but-empty one", () => {
    expect(logsView({ enabled: false, capacity: 0, returned: 0, lines: [] })).toBe("disabled");
    expect(logsView({ enabled: true, capacity: 200, returned: 0, lines: [] })).toBe("empty");
    expect(logsView({ enabled: true, capacity: 200, returned: 1, lines: [{ message: "hi" }] })).toBe("lines");
  });
});

describe("DiagnosticsView rendering", () => {
  it("always identifies the inspected member in the header", () => {
    const html = render({
      member: { slug: "roxy-9f", name: "roxy", presenceKey: "stopped", presenceLabel: "stopped" },
    });
    expect(html).toContain("roxy");
    expect(html).toContain("roxy-9f");
    expect(html).toContain('aria-label="Diagnostics for roxy"');
  });

  // Substrings deliberately avoid apostrophes — react-dom/server escapes `'` to `&#x27;`.
  const failureCases: [DiagFailureKind, string][] = [
    ["stopped", "is stopped"],
    ["unreachable", "reach ava"],
    ["timeout", "timed out"],
    ["unauthorized", "Not authorized"],
    ["disabled", "Diagnostics unavailable"],
  ];
  it.each(failureCases)("renders the %s state as an actionable logs card", (kind, needle) => {
    expect(render({ logs: { status: "error", kind } })).toContain(needle);
  });

  it("renders a missing-task error only for the inspected-task panel", () => {
    expect(render({ task: { status: "error", kind: "missing-task" } })).toContain("No such task");
  });

  it("renders bounded log lines with the member's clamp note and the retained-count meta", () => {
    const logs: DiagnosticsLogs = {
      enabled: true,
      capacity: 200,
      returned: 2,
      lines: [
        { ts: "2026-08-28T12:00:00+00:00", level: "ERROR", logger: "graph.agent", message: "boom happened" },
        { message: "a second line" },
      ],
      note: "lines=99999 above maximum; using 1000",
    };
    const html = render({ logs: { status: "ready", data: logs } });
    expect(html).toContain("boom happened");
    expect(html).toContain("ERROR");
    expect(html).toContain("above maximum"); // the clamp note is surfaced, not swallowed
    expect(html).toContain("of ≤200 retained");
  });

  it("shows the disabled-buffer opt-out with its note, distinct from an empty ring", () => {
    const disabled = render({
      logs: {
        status: "ready",
        data: { enabled: false, capacity: 0, returned: 0, lines: [], note: "log buffer disabled (LOG_BUFFER_LINES=0)" },
      },
    });
    expect(disabled).toContain("Log buffer disabled");
    expect(disabled).toContain("LOG_BUFFER_LINES=0");

    const empty = render({
      logs: { status: "ready", data: { enabled: true, capacity: 200, returned: 0, lines: [] } },
    });
    expect(empty).toContain("No log lines yet");
  });

  it("prompts for an exact task id before one is inspected", () => {
    expect(render({ task: null })).toContain("Enter an exact task id");
  });

  it("renders the #3168 task summary and surfaces truncated + malformed metadata verbatim", () => {
    const task: DiagnosticsTask = {
      task_id: "task-1",
      context_id: "ctx-1",
      state: "completed",
      status_message: "all done",
      last_updated: "2026-08-28T12:00:00+00:00",
      history: [{ role: "user", message_id: "m1", text: "the question" }],
      artifacts: [{ artifact_id: "a1", name: "answer", text: "the answer" }],
      accumulated_text: "the answer",
      truncated: ["history"],
      malformed: ["status"],
    };
    const html = render({ task: { status: "ready", data: task } });
    expect(html).toContain("completed");
    expect(html).toContain("task-1");
    expect(html).toContain("the answer");
    expect(html).toContain("Truncated: history");
    expect(html).toContain("Unparsed: status");
  });
});

describe("drawer-local member invariant (r3)", () => {
  it("pins the inspected member drawer-locally, never off the fleet selection", () => {
    // Opening the drawer pins the CLICKED member's slug+name — not the composer `target`
    // or the focused-window `here` — so a fleet switch can't retarget the drawer.
    expect(fleetRoomSource).toMatch(/setDiag\(\{[^}]*slug:\s*slugOf\(a\)[^}]*name:\s*a\.name[^}]*\}\)/);
    // The drawer is keyed by the pinned slug, so a 3s fleet poll re-render never remounts it.
    expect(fleetRoomSource).toContain("key={diag.slug}");
  });

  it("is snapshot-refresh only — no polling or SSE following of the log stream", () => {
    // The diagnostics queries must not carry a refetchInterval (that lives in queries.ts's
    // fleetQuery, imported), and must hold their snapshot until an explicit refetch.
    expect(fleetRoomSource).not.toContain("refetchInterval");
    expect(fleetRoomSource).toContain("staleTime: Infinity");
  });
});
