import { describe, expect, it, vi } from "vitest";

import {
  requiresGoalCloseConfirmation,
  resolveGoalCloseDisposition,
  sessionsToClose,
} from "./bulkClose";

const sessions = [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }, { id: "e" }];

describe("sessionsToClose", () => {
  it("'others' returns every tab except the anchor", () => {
    expect(sessionsToClose(sessions, "c", "others")).toEqual(["a", "b", "d", "e"]);
  });

  it("'left' returns only the tabs before the anchor, in order", () => {
    expect(sessionsToClose(sessions, "c", "left")).toEqual(["a", "b"]);
  });

  it("'right' returns only the tabs after the anchor, in order", () => {
    expect(sessionsToClose(sessions, "c", "right")).toEqual(["d", "e"]);
  });

  it("'left' on the first tab is empty (nothing to the left)", () => {
    expect(sessionsToClose(sessions, "a", "left")).toEqual([]);
  });

  it("'right' on the last tab is empty (nothing to the right)", () => {
    expect(sessionsToClose(sessions, "e", "right")).toEqual([]);
  });

  it("never includes the anchor itself", () => {
    for (const mode of ["others", "left", "right"] as const) {
      expect(sessionsToClose(sessions, "c", mode)).not.toContain("c");
    }
  });

  it("returns empty for an unknown anchor (nothing to close)", () => {
    expect(sessionsToClose(sessions, "zzz", "others")).toEqual([]);
    expect(sessionsToClose(sessions, "zzz", "left")).toEqual([]);
    expect(sessionsToClose(sessions, "zzz", "right")).toEqual([]);
  });

  it("returns empty for a single-tab strip in every mode", () => {
    const one = [{ id: "only" }];
    expect(sessionsToClose(one, "only", "others")).toEqual([]);
    expect(sessionsToClose(one, "only", "left")).toEqual([]);
    expect(sessionsToClose(one, "only", "right")).toEqual([]);
  });
});

describe("requiresGoalCloseConfirmation", () => {
  const goals = [
    { session_id: "goal-live", status: "active" },
    { session_id: "goal-done", status: "completed" },
  ];

  it("routes an active goal through Stop-vs-Detach even for a direct close gesture", () => {
    expect(requiresGoalCloseConfirmation(goals, "goal-live")).toBe(true);
  });

  it("keeps direct retirement for non-goal and inactive-goal chats", () => {
    expect(requiresGoalCloseConfirmation(goals, "ordinary-chat")).toBe(false);
    expect(requiresGoalCloseConfirmation(goals, "goal-done")).toBe(false);
  });
});

describe("resolveGoalCloseDisposition", () => {
  const activeGoal = { session_id: "goal-live", status: "active" };
  const completedGoal = { session_id: "goal-done", status: "completed" };

  it("waits for ownership on a fresh mount instead of treating unresolved data as empty", async () => {
    let resolveGoals!: (goals: typeof activeGoal[]) => void;
    const goals = new Promise<typeof activeGoal[]>((resolve) => {
      resolveGoals = resolve;
    });
    let disposition: string | undefined;

    const resolving = resolveGoalCloseDisposition("goal-live", () => goals).then((result) => {
      disposition = result;
    });
    await Promise.resolve();
    expect(disposition).toBeUndefined();

    resolveGoals([activeGoal]);
    await resolving;
    expect(disposition).toBe("confirm-goal");
  });

  it("uses the authoritative refetch rather than a stale non-goal cache", async () => {
    const staleGoals = [completedGoal];
    const refreshGoals = vi.fn().mockResolvedValue([activeGoal]);

    expect(requiresGoalCloseConfirmation(staleGoals, "goal-live")).toBe(false);
    await expect(resolveGoalCloseDisposition("goal-live", refreshGoals)).resolves.toBe(
      "confirm-goal",
    );
    expect(refreshGoals).toHaveBeenCalledOnce();
  });

  it("allows direct close only after a loaded result proves no active owner", async () => {
    await expect(
      resolveGoalCloseDisposition("ordinary-chat", async () => [activeGoal, completedGoal]),
    ).resolves.toBe("direct");
    await expect(
      resolveGoalCloseDisposition("goal-done", async () => [activeGoal, completedGoal]),
    ).resolves.toBe("direct");
  });

  it("fails closed when ownership cannot be loaded", async () => {
    await expect(resolveGoalCloseDisposition("chat", async () => undefined)).resolves.toBe(
      "blocked",
    );
    await expect(
      resolveGoalCloseDisposition("chat", async () => {
        throw new Error("offline");
      }),
    ).resolves.toBe("blocked");
  });
});
