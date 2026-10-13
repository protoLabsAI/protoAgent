import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { queryKeys, refreshGoalsOnEvent } from "./queries";

// Review of #4008: the app-wide goal.* refresh invalidated the `goals` PREFIX with
// refetchType "all", and goal-detail queries live under that prefix — so every goal event
// refetched every cached (closed) detail drawer. Only the list refetches unconditionally;
// a detail refetches only while a drawer observes it.
describe("refreshGoalsOnEvent", () => {
  it("refetches the goals list (even unobserved) and only ACTIVE detail queries", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    const list = vi.fn(async () => ({ goals: [] }));
    const closedDetail = vi.fn(async () => ({ id: "closed" }));
    const openDetail = vi.fn(async () => ({ id: "open" }));

    await qc.fetchQuery({ queryKey: queryKeys.goals, queryFn: list });
    await qc.fetchQuery({ queryKey: queryKeys.goalDetail("closed"), queryFn: closedDetail });
    const observer = new QueryObserver(qc, { queryKey: queryKeys.goalDetail("open"), queryFn: openDetail });
    const unsub = observer.subscribe(() => {});
    await vi.waitFor(() => expect(observer.getCurrentResult().isSuccess).toBe(true));
    list.mockClear();
    closedDetail.mockClear();
    openDetail.mockClear();

    refreshGoalsOnEvent(qc); // one goal event

    await vi.waitFor(() => expect(list).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(openDetail).toHaveBeenCalledTimes(1));
    await new Promise((r) => setTimeout(r, 10));
    expect(list).toHaveBeenCalledTimes(1);
    expect(closedDetail).not.toHaveBeenCalled(); // an inactive drawer is only marked stale
    expect(qc.getQueryState(queryKeys.goalDetail("closed"))?.isInvalidated).toBe(true);
    unsub();
    qc.clear();
  });
});
