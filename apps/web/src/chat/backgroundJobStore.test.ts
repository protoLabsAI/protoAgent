import { beforeEach, describe, expect, it, vi } from "vitest";

// The chat's view of background jobs: hydrated from the API, kept live by the bus. The
// ordering guard is the point — a hydration that started before a completion landed must
// not put the job back to "running".

const listeners: Record<string, (d: Record<string, unknown>) => void> = {};
let backgroundCall: () => Promise<{ enabled: boolean; jobs: unknown[] }>;

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
    fn(true);
    return () => {};
  },
}));

const { __resetForTest, runningJobsFor, subscribeForTest, snapshotForTest } = await import("./backgroundJobStore");

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
