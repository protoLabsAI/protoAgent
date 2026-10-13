import { describe, expect, it, vi } from "vitest";

import { NO_MEMORY_CHANGE, canClearSession, defaultMemoryChoice, retireChatSession } from "./sessionRetirement";

describe("defaultMemoryChoice (#4053)", () => {
  it("harvests a regular chat by default, never forgetting", () => {
    expect(defaultMemoryChoice(false)).toEqual({ harvest: true, forget: false });
    // No flag given (the common call shape) is treated as a regular chat.
    expect(defaultMemoryChoice()).toEqual({ harvest: true, forget: false });
  });

  it("never harvests an incognito chat", () => {
    expect(defaultMemoryChoice(true)).toEqual({ harvest: false, forget: false });
  });

  it("is distinct from NO_MEMORY_CHANGE, which still touches nothing (goal-tab closes)", () => {
    expect(NO_MEMORY_CHANGE).toEqual({ harvest: false, forget: false });
    expect(defaultMemoryChoice(false)).not.toEqual(NO_MEMORY_CHANGE);
  });
});

describe("retireChatSession", () => {
  it("removes the local handle only after durable retirement succeeds", async () => {
    const order: string[] = [];
    await retireChatSession("chat-a", { harvest: true, forget: false }, {
      retireRemote: async () => { order.push("remote"); },
      deleteLocal: () => { order.push("local"); },
    });
    expect(order).toEqual(["remote", "local"]);
  });

  it("forwards both memory choices to the server delete (#3493)", async () => {
    const retireRemote = vi.fn().mockResolvedValue({ deleted: true });
    await retireChatSession("chat-a", { harvest: false, forget: true }, { retireRemote, deleteLocal: () => {} });
    expect(retireRemote).toHaveBeenCalledWith("chat-a", { harvest: false, forget: true });
  });

  it("preserves the local handle after failure so the same action can retry", async () => {
    const deleteLocal = vi.fn();
    const retireRemote = vi.fn()
      .mockRejectedValueOnce(new Error("tombstone unavailable"))
      .mockResolvedValueOnce({ deleted: true });
    const deps = { retireRemote, deleteLocal };

    await expect(retireChatSession("chat-a", NO_MEMORY_CHANGE, deps)).rejects.toThrow("tombstone unavailable");
    expect(deleteLocal).not.toHaveBeenCalled();

    await retireChatSession("chat-a", NO_MEMORY_CHANGE, deps);
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
