import { beforeEach, describe, expect, it, vi } from "vitest";

import type { BackgroundJobDTO } from "../lib/types";

// The chat's view of background jobs: hydrated from the API, kept live by the bus. The
// ordering guard is the point — a hydration that started before a completion landed must
// not put the job back to "running".

const listeners: Record<string, (d: Record<string, unknown>) => void> = {};
let backgroundCall: () => Promise<{ enabled: boolean; jobs: unknown[] }>;
let connListener: ((c: boolean) => void) | null = null;

vi.mock("../lib/api", () => ({
  api: {
    background: () => backgroundCall(),
    backgroundJob: () => Promise.reject(new Error("not used here")),
  },
}));
vi.mock("../lib/events", () => ({
  onTopic: (topic: string, fn: (d: Record<string, unknown>) => void) => {
    listeners[topic] = fn;
    return () => delete listeners[topic];
  },
  onConnectionChange: (fn: (c: boolean) => void) => {
    connListener = fn;
    fn(true);
    return () => {};
  },
}));

const { __resetForTest, jobIdOf, mergeHydration, runningJobsFor, subscribeForTest, snapshotForTest } = await import(
  "./backgroundJobStore"
);

const row = (over: Record<string, unknown> = {}) => ({
  id: "bg-1",
  status: "running",
  subagent_type: "delegate",
  description: "delegate → sonnet: Land PR #13",
  origin_session: "s1",
  ...over,
});

describe("a hydration never overwrites a newer live event", () => {
  beforeEach(() => {
    __resetForTest();
    for (const k of Object.keys(listeners)) delete listeners[k];
  });

  it("keeps the completion that landed while the request was in flight", async () => {
    let resolveHydrate: (v: { enabled: boolean; jobs: unknown[] }) => void = () => {};
    backgroundCall = () => new Promise((r) => (resolveHydrate = r));
    subscribeForTest(() => {});

    // The completion arrives BEFORE the (slow) list response, which still says "running".
    listeners["background.completed"]({ job_id: "bg-1", status: "completed", origin_session: "s1" });
    resolveHydrate({ enabled: true, jobs: [row()] });
    await new Promise((r) => setTimeout(r, 0));

    expect(snapshotForTest()["bg-1"].status).toBe("completed");
    expect(runningJobsFor(snapshotForTest(), "s1")).toEqual([]);
  });

  it("still takes rows the live bus never mentioned", async () => {
    backgroundCall = () => Promise.resolve({ enabled: true, jobs: [row({ id: "bg-2" })] });
    subscribeForTest(() => {});
    await new Promise((r) => setTimeout(r, 0));
    expect(runningJobsFor(snapshotForTest(), "s1").map((j) => j.id)).toEqual(["bg-2"]);
  });

  it("keeps a job the API no longer lists (it returns only the first page)", async () => {
    backgroundCall = () => Promise.resolve({ enabled: true, jobs: [] });
    subscribeForTest(() => {});
    listeners["background.started"]({ job_id: "bg-3", origin_session: "s1", subagent_type: "delegate" });
    await new Promise((r) => setTimeout(r, 0));
    expect(runningJobsFor(snapshotForTest(), "s1").map((j) => j.id)).toEqual(["bg-3"]);
  });
});

/** A list row typed as the API's DTO (the merge's input). */
const listed = (over: Record<string, unknown> = {}) => row(over) as unknown as BackgroundJobDTO;

// A delegate's live snapshot, as `background.progress` carries it (phase delegate_progress).
const snapshot = (over: Record<string, unknown> = {}) => ({
  target: "protoEngineer",
  current_tool: null,
  recent_tools: [{ id: "t1", name: "board_create_feature", status: "completed" }],
  tool_count: 1,
  done: false,
  ok: true,
  ...over,
});

describe("a hydration MERGES into what the bus already said (the progress-card flicker)", () => {
  beforeEach(() => {
    __resetForTest();
    for (const k of Object.keys(listeners)) delete listeners[k];
  });

  it("keeps a running job's live progress across a reconnect's re-hydration", async () => {
    // Every bus reconnect re-hydrates. The list has no progress; it must not erase it.
    backgroundCall = () => Promise.resolve({ enabled: true, jobs: [] });
    subscribeForTest(() => {});
    listeners["background.started"]({ job_id: "bg-1", origin_session: "s1", subagent_type: "delegate" });
    listeners["background.progress"]({ job_id: "bg-1", phase: "delegate_progress", progress: snapshot() });
    await new Promise((r) => setTimeout(r, 0));
    expect(snapshotForTest()["bg-1"].progress?.recentTools).toHaveLength(1);

    // The bus drops and comes back (a member's stream used to end every 15s idle): the
    // reconnect re-hydrates from a list that has the job running and no progress.
    let seen = 0;
    subscribeForTest(() => {
      if (!snapshotForTest()["bg-1"]?.progress) seen++;
    });
    backgroundCall = () => Promise.resolve({ enabled: true, jobs: [row()] });
    connListener?.(false);
    connListener?.(true);
    await new Promise((r) => setTimeout(r, 0));
    expect(snapshotForTest()["bg-1"].progress?.recentTools.map((t) => t.name)).toEqual(["board_create_feature"]);
    expect(snapshotForTest()["bg-1"].status).toBe("running");
    expect(seen).toBe(0); // never, not even for one emit, without its progress
  });

  it("is the same map when the list says nothing new — no re-render, no remount", () => {
    const cur = { "bg-1": { ...listed(), status: "running" as const } };
    expect(mergeHydration(cur, [listed()])).toBe(cur);
  });

  it("never drops a job the page doesn't list", () => {
    const cur = { "bg-9": { ...listed({ id: "bg-9" }), status: "running" as const } };
    const next = mergeHydration(cur, [listed({ id: "bg-2" })]);
    expect(Object.keys(next).sort()).toEqual(["bg-2", "bg-9"]);
  });

  it("takes the list's newer status, keeping the rest", () => {
    const cur = { "bg-1": { ...listed(), status: "running" as const, progress: undefined } };
    expect(mergeHydration(cur, [listed({ status: "completed" })])["bg-1"].status).toBe("completed");
  });

  it("keys a row by `id` (REST) or `job_id` (bus shape) — one job, one entry", () => {
    expect(jobIdOf({ id: "bg-1" })).toBe("bg-1");
    expect(jobIdOf({ job_id: "bg-1" })).toBe("bg-1");
    expect(jobIdOf({})).toBe("");
    const next = mergeHydration({}, [{ ...listed({ id: undefined }), job_id: "bg-1" }]);
    expect(Object.keys(next)).toEqual(["bg-1"]);
  });

  it("skips a row a live event saw later", () => {
    const cur = { "bg-1": { ...listed(), status: "completed" as const } };
    expect(mergeHydration(cur, [listed()], (id) => id === "bg-1")).toBe(cur);
  });
});
