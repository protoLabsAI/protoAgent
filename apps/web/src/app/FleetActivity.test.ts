// Fleet Room roster state from member bus frames (#4290). A member running a turn another
// agent DELEGATED over A2A announces it with `turn.started { origin: "a2a", task_id, caller,
// summary }` and ends it with `turn.finished` / `turn.usage`; the room tracks that work by
// task id (so parallel delegations show at once) while every other turn keeps the original
// +1/−1 count. Drives the exported reducer directly — no SSE, no mount.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { applyMemberFrame, resetFleetActivityForTests, useMemberRunning, useMemberWork } from "./FleetActivity";
import { WorkLine } from "./FleetRoom";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The selector hooks are what the roster reads, so observe the state through them.
let snap: { work: ReturnType<typeof useMemberWork>; running: ReturnType<typeof useMemberRunning> };
function Probe() {
  snap = { work: useMemberWork(), running: useMemberRunning() };
  return null;
}

let container: HTMLElement;
let root: Root;

function frame(slug: string, topic: string, data: Record<string, unknown>) {
  act(() => applyMemberFrame(slug, topic, data));
}

const started = (task: string, extra: Record<string, unknown> = {}) => ({
  session_id: `ctx-${task}`,
  task_id: task,
  origin: "a2a",
  trigger: "delegate_to",
  caller: "hub",
  summary: `do ${task}`,
  ...extra,
});

beforeEach(() => {
  resetFleetActivityForTests();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(h(Probe)));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetFleetActivityForTests();
});

describe("delegated (A2A) turns", () => {
  it("shows the member busy with who asked and what for, then idle on the terminal frame", () => {
    frame("ava", "turn.started", started("t1"));
    expect(snap.work.ava?.t1).toMatchObject({ taskId: "t1", caller: "hub", summary: "do t1" });

    frame("ava", "turn.usage", { task_id: "t1", context_id: "ctx-t1", state: "completed" });
    expect(snap.work.ava).toBeUndefined();
    // turn.finished after the usage line is a no-op, and never touches the plain count.
    frame("ava", "turn.finished", { task_id: "t1", session_id: "ctx-t1", origin: "a2a", state: "completed" });
    expect(snap.work.ava).toBeUndefined();
    expect(snap.running.ava).toBeUndefined();
  });

  it("ends on turn.finished alone (a crash path that never priced the turn)", () => {
    frame("ava", "turn.started", started("t1"));
    frame("ava", "turn.finished", { task_id: "t1", origin: "a2a", state: "failed", ok: false });
    expect(snap.work.ava).toBeUndefined();
  });

  it("ends on a cancel", () => {
    frame("ava", "turn.started", started("t1"));
    frame("ava", "turn.usage", { task_id: "t1", state: "canceled" });
    expect(snap.work.ava).toBeUndefined();
  });

  it("shows parallel delegations to several members — and two to one member — at once", () => {
    frame("ava", "turn.started", started("t1"));
    frame("roxy", "turn.started", started("t2"));
    frame("ava", "turn.started", started("t3", { caller: "lead" }));
    expect(Object.keys(snap.work.ava ?? {}).sort()).toEqual(["t1", "t3"]);
    expect(Object.keys(snap.work.roxy ?? {})).toEqual(["t2"]);

    frame("ava", "turn.usage", { task_id: "t1", state: "completed" });
    expect(Object.keys(snap.work.ava ?? {})).toEqual(["t3"]);
    expect(Object.keys(snap.work.roxy ?? {})).toEqual(["t2"]);
  });

  it("a delegated turn's end never decrements a concurrent counted turn", () => {
    frame("ava", "turn.started", { session_id: "s", origin: "scheduler" }); // counted +1
    frame("ava", "turn.started", started("t1"));
    frame("ava", "turn.usage", { task_id: "t1", state: "completed" });
    expect(snap.running.ava).toBe(1); // the scheduled turn is still running
  });

  it("a park hands the work to 'needs approval' and its resume puts the same work back", () => {
    frame("ava", "turn.started", started("t1"));
    // The park leg's own spend line lands first — still parked, not finished.
    frame("ava", "turn.usage", { task_id: "t1", state: "TASK_STATE_INPUT_REQUIRED" });
    expect(snap.work.ava?.t1).toBeDefined();
    frame("ava", "turn.input_required", { task_id: "t1", context_id: "ctx-t1", prompt: "Which repo?" });
    expect(snap.work.ava).toBeUndefined();

    frame("ava", "turn.resumed", { task_id: "t1", context_id: "ctx-t1" });
    expect(snap.work.ava?.t1).toMatchObject({ caller: "hub", summary: "do t1" });
    expect(snap.running.ava).toBeUndefined(); // tracked by id, not counted

    frame("ava", "turn.usage", { task_id: "t1", state: "completed" });
    expect(snap.work.ava).toBeUndefined();
  });
});

describe("other turns keep the +1/−1 count", () => {
  it("counts an autonomous turn.started and its terminal turn.usage", () => {
    frame("ava", "turn.started", { session_id: "s", origin: "scheduler" });
    expect(snap.running.ava).toBe(1);
    frame("ava", "turn.finished", { session_id: "s" }); // not counted
    expect(snap.running.ava).toBe(1);
    frame("ava", "turn.usage", { task_id: "x", state: "completed" });
    expect(snap.running.ava).toBeUndefined();
  });
});

describe("WorkLine", () => {
  it("names the caller and the ask, and collapses parallel work to the newest plus a count", () => {
    act(() =>
      root.render(
        h(WorkLine, {
          items: [
            { taskId: "a", caller: "hub", summary: "older ask", startedAt: 1 },
            { taskId: "b", caller: "lead", summary: "newest ask", startedAt: 2 },
          ],
        }),
      ),
    );
    const text = container.textContent || "";
    expect(text).toContain("for lead");
    expect(text).toContain("newest ask");
    expect(text).toContain("+1 more");
    expect(text).not.toContain("older ask");
  });

  it("renders nothing when idle and a generic label when the caller is unnamed", () => {
    act(() => root.render(h(WorkLine, { items: [] })));
    expect(container.textContent).toBe("");
    act(() => root.render(h(WorkLine, { items: [{ taskId: "a", caller: "", summary: "", startedAt: 1 }] })));
    expect(container.textContent).toBe("delegated task");
  });
});
