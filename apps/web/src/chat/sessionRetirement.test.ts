import { describe, expect, it, vi } from "vitest";

import { canClearSession, retireChatSession } from "./sessionRetirement";

describe("retireChatSession", () => {
  it("removes the local handle only after durable retirement succeeds", async () => {
    const order: string[] = [];
    await retireChatSession("chat-a", true, {
      retireRemote: async () => { order.push("remote"); },
      deleteLocal: () => { order.push("local"); },
    });
    expect(order).toEqual(["remote", "local"]);
  });

  it("preserves the local handle after failure so the same action can retry", async () => {
    const deleteLocal = vi.fn();
    const retireRemote = vi.fn()
      .mockRejectedValueOnce(new Error("tombstone unavailable"))
      .mockResolvedValueOnce({ deleted: true });
    const deps = { retireRemote, deleteLocal };

    await expect(retireChatSession("chat-a", false, deps)).rejects.toThrow("tombstone unavailable");
    expect(deleteLocal).not.toHaveBeenCalled();

    await retireChatSession("chat-a", false, deps);
    expect(retireRemote).toHaveBeenCalledTimes(2);
    expect(deleteLocal).toHaveBeenCalledWith("chat-a");
  });
});

describe("canClearSession", () => {
  it("disallows clear while a producer can still save the pre-clear turn", () => {
    expect(canClearSession("streaming")).toBe(false);
    expect(canClearSession("idle", true)).toBe(false);
    expect(canClearSession("idle")).toBe(true);
    expect(canClearSession("error")).toBe(true);
  });
});
