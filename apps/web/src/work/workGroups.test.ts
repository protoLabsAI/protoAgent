// @vitest-environment node
import { describe, expect, it } from "vitest";
import type { BackgroundJobDTO, GoalState, Task } from "../lib/types";
import type { DirectiveRow } from "../lib/api/chat";
import { fromBackgroundJob, fromDirective, fromGoal, fromTask, statusTone, type GroupableWorkItem } from "./tone";
import { workGroups } from "./workGroups";

const base = { id: "job", title: "Job", meta: "", sessionId: "chat" };
function task(status: string, sessionId: string | undefined = "chat"): GroupableWorkItem {
  const raw: Task = { id: "job", title: "Job", status };
  return { ...base, sessionId, kind: "task", status: fromTask(status), raw };
}
function goal(status: string): GroupableWorkItem {
  const raw: GoalState = { session_id: "chat", condition: "Finish", status };
  return { ...base, id: "chat", kind: "goal", status: fromGoal(status), raw };
}
function background(status: BackgroundJobDTO["status"]): GroupableWorkItem {
  const raw: BackgroundJobDTO = { id: "job", status, description: "Job", subagent_type: "coder", origin_session: "chat" };
  return { ...base, kind: "background", status: fromBackgroundJob(status), raw };
}
function directive(): GroupableWorkItem {
  const raw: DirectiveRow = {
    id: "d", session_id: "chat", quote: "Go", conditions: "", scope: "Job",
    status: "in_force", created_at: "2026-11-11T12:00:00Z", closed_at: null, closed_by: null, close_reason: null,
  };
  return { ...base, id: raw.id, kind: "directive", status: fromDirective(raw.status), raw };
}

describe("shared workGroups projection", () => {
  it("always returns four ordered groups with counts derived from their rows", () => {
    const groups = workGroups([task("open"), task("deferred"), task("waiting"), task("in_progress"), background("completed"), goal("achieved"), directive()]);
    expect(groups.map(({ id, label, count }) => [id, label, count])).toEqual([
      ["needs-you", "Needs you", 1], ["doing-now", "Doing now", 1],
      ["coming-up", "Coming up", 2], ["instructions", "Your instructions", 1],
    ]);
    for (const group of groups) expect(group.count).toBe(group.items.length);
    expect(workGroups([]).map((g) => g.count)).toEqual([0, 0, 0, 0]);
  });

  it("filters other groups to the same session but never filters Needs you", () => {
    const decision: GroupableWorkItem = { ...base, kind: "decision", sessionId: "elsewhere", status: "needs-you", raw: { state: "pending" } };
    const delivery: GroupableWorkItem = { ...base, kind: "delivery", sessionId: "elsewhere", status: "needs-you", raw: { delivered: false, needsHuman: true } };
    const items = [decision, delivery, { ...goal("exhausted"), sessionId: "elsewhere" }, task("in_progress"), task("in_progress", "other"),
      { ...task("open"), sessionId: undefined }, task("deferred"), directive(), { ...directive(), sessionId: "other" }];
    expect(workGroups(items, { thisChatOnly: true, sessionId: "chat" }).map((g) => g.count)).toEqual([3, 1, 1, 1]);
    expect(workGroups(items, { thisChatOnly: true }).map((g) => g.count)).toEqual([3, 0, 0, 0]);
    expect(workGroups(items).map((g) => g.count)).toEqual([3, 2, 2, 2]);
  });

  it.each(["exhausted", "unachievable"])("projects %s goals neutrally without changing raw or cached rows", (status) => {
    const item = Object.freeze(goal(status));
    const projected = workGroups([item])[0].items[0];
    expect(statusTone(projected.status)).toBe("neutral");
    expect(projected.raw).toBe(item.raw);
    expect(item.status).toBe("failed");
  });

  it.each([
    [task("in_progress"), task("closed")],
    [background("running"), background("completed")],
    [background("running"), background("failed")],
    [background("running"), background("canceled")],
    [goal("active"), goal("achieved")],
    [goal("active"), goal("cleared")],
    [goal("active"), goal("exhausted")],
    [goal("active"), goal("unachievable")],
  ])("removes explicitly tied instructions as the job ends (%j)", (running, ended) => {
    const tied = { ...directive(), tiedJob: { kind: running.kind as "task" | "background" | "goal", id: running.id } };
    const unrelated = directive();
    expect(workGroups([running, tied, unrelated]).map((g) => g.count)).toEqual([0, 1, 0, 2]);
    const after = workGroups([ended, tied, unrelated]);
    expect(after[1].count).toBe(0);
    expect(after[3].items).toEqual([unrelated]);
    expect((tied.raw as DirectiveRow).status).toBe("in_force");
  });

  it("does not infer completion from missing jobs or associations from shared text/session/id", () => {
    const tied = { ...directive(), tiedJob: { kind: "background" as const, id: "job" } };
    expect(workGroups([tied])[3].items).toEqual([tied]);
    expect(workGroups([task("closed"), tied, directive()])[3].count).toBe(2);
    expect(workGroups([background("completed"), directive()])[3].count).toBe(1);
  });

  it("projects job endings before session filtering", () => {
    const tied = { ...directive(), tiedJob: { kind: "background" as const, id: "job" } };
    const ended = { ...background("completed"), sessionId: "other" };
    expect(workGroups([ended, tied], { thisChatOnly: true, sessionId: "chat" })[3].count).toBe(0);
  });
});
