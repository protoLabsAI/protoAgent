// The cross-tab turn-owner marker (#5514). "Another tab" is a marker under a different tab id.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { matchKey, readKey, removeKey, writeKey } from "../lib/storage";
import {
  __resetTurnOwnerForTests,
  claimTurn,
  ownedByAnotherLiveTab,
  TURN_OWNER_HEARTBEAT_MS,
  TURN_OWNER_STALE_MS,
  turnOwnerKey,
  turnOwnerTabId,
  whenTurnFree,
} from "./turnOwner";

const SESSION = "session-1";
const TASK = "task-1";
const KEY = turnOwnerKey(SESSION, TASK);

/** What another tab's claim (or heartbeat) writes. */
function otherTabStamps(at = Date.now(), key = KEY) {
  writeKey("local", key, JSON.stringify({ tab: "another-tab", at }));
}

function marker(key = KEY): { tab: string; at: number } | null {
  const raw = readKey("local", key);
  return raw ? JSON.parse(raw) : null;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-11-22T12:00:00Z"));
});

afterEach(() => {
  __resetTurnOwnerForTests();
  removeKey("local", KEY);
  vi.useRealTimers();
});

describe("ownedByAnotherLiveTab", () => {
  it("is true while another tab's marker is renewed, and false once it is released", () => {
    otherTabStamps();
    expect(ownedByAnotherLiveTab(SESSION, TASK)).toBe(true);
    // The owner keeps renewing: still owned well past the stale window.
    for (let elapsed = 0; elapsed < 3 * TURN_OWNER_STALE_MS; elapsed += TURN_OWNER_HEARTBEAT_MS) {
      vi.advanceTimersByTime(TURN_OWNER_HEARTBEAT_MS);
      otherTabStamps();
      expect(ownedByAnotherLiveTab(SESSION, TASK)).toBe(true);
    }
    removeKey("local", KEY); // the owner's stream ended
    expect(ownedByAnotherLiveTab(SESSION, TASK)).toBe(false);
  });

  it("is false once the other tab's heartbeat goes stale", () => {
    otherTabStamps();
    vi.advanceTimersByTime(TURN_OWNER_STALE_MS - 1);
    expect(ownedByAnotherLiveTab(SESSION, TASK)).toBe(true);
    vi.advanceTimersByTime(1);
    expect(ownedByAnotherLiveTab(SESSION, TASK)).toBe(false);
  });

  it("never counts this tab's own marker as another tab, and release removes it", () => {
    const release = claimTurn(SESSION, TASK);
    expect(marker()?.tab).toBe(turnOwnerTabId());
    expect(ownedByAnotherLiveTab(SESSION, TASK)).toBe(false);
    vi.advanceTimersByTime(TURN_OWNER_HEARTBEAT_MS);
    expect(ownedByAnotherLiveTab(SESSION, TASK)).toBe(false);
    release();
    expect(marker()).toBeNull();
  });

  it("is false with no marker, and scoped to the session and the task", () => {
    expect(ownedByAnotherLiveTab(SESSION, TASK)).toBe(false);
    otherTabStamps();
    expect(ownedByAnotherLiveTab(SESSION, "task-2")).toBe(false);
    expect(ownedByAnotherLiveTab("session-2", TASK)).toBe(false);
  });

  it("reads a malformed marker, or one stamped far in the future, as nobody's", () => {
    writeKey("local", KEY, "not json");
    expect(ownedByAnotherLiveTab(SESSION, TASK)).toBe(false);
    writeKey("local", KEY, JSON.stringify({ tab: "another-tab" }));
    expect(ownedByAnotherLiveTab(SESSION, TASK)).toBe(false);
    otherTabStamps(Date.now() + TURN_OWNER_STALE_MS);
    expect(ownedByAnotherLiveTab(SESSION, TASK)).toBe(false);
  });
});

describe("claimTurn", () => {
  it("renews the marker on an interval until released", () => {
    const release = claimTurn(SESSION, TASK);
    const first = marker()!.at;
    vi.advanceTimersByTime(TURN_OWNER_HEARTBEAT_MS);
    expect(marker()!.at).toBe(first + TURN_OWNER_HEARTBEAT_MS);
    vi.advanceTimersByTime(5 * TURN_OWNER_HEARTBEAT_MS);
    expect(Date.now() - marker()!.at).toBeLessThan(TURN_OWNER_STALE_MS);
    release();
    vi.advanceTimersByTime(2 * TURN_OWNER_HEARTBEAT_MS);
    expect(marker()).toBeNull(); // no heartbeat writes it back
  });

  it("renew() re-stamps the marker between heartbeats, and not after release", () => {
    const release = claimTurn(SESSION, TASK);
    vi.advanceTimersByTime(500);
    release.renew();
    expect(marker()!.at).toBe(Date.now());
    release();
    release.renew();
    expect(marker()).toBeNull();
  });

  it("release is idempotent and keeps the marker while a second claim in this tab holds it", () => {
    const first = claimTurn(SESSION, TASK);
    const second = claimTurn(SESSION, TASK);
    first();
    first();
    expect(marker()?.tab).toBe(turnOwnerTabId());
    second();
    expect(marker()).toBeNull();
  });

  it("release leaves a marker another tab took over", () => {
    const release = claimTurn(SESSION, TASK);
    otherTabStamps();
    release();
    expect(marker()?.tab).toBe("another-tab");
  });

  it("releases on pagehide, and re-stamps if the page comes back", () => {
    const release = claimTurn(SESSION, TASK);
    window.dispatchEvent(new Event("pagehide"));
    expect(marker()).toBeNull();
    vi.advanceTimersByTime(TURN_OWNER_HEARTBEAT_MS); // restored from the back/forward cache
    expect(marker()?.tab).toBe(turnOwnerTabId());
    release();
  });

  it("sweeps markers a dead tab never released, leaving live ones", () => {
    const dead = turnOwnerKey("session-dead", "task-dead");
    const live = turnOwnerKey("session-live", "task-live");
    otherTabStamps(Date.now() - TURN_OWNER_STALE_MS, dead);
    otherTabStamps(Date.now(), live);
    const release = claimTurn(SESSION, TASK);
    expect(marker(dead)).toBeNull();
    expect(marker(live)?.tab).toBe("another-tab");
    release();
    removeKey("local", live);
  });

  it("writes a registered, never-evicted key, whatever the ids contain", () => {
    for (const key of [KEY, turnOwnerKey("a:b/c", "task:with:colons")]) {
      expect(matchKey("local", key)?.spec).toMatchObject({
        id: "chat.turnOwner",
        category: "ephemeral",
        evictable: false,
      });
    }
    expect(turnOwnerKey("a:b", "c")).not.toBe(turnOwnerKey("a", "b:c"));
  });

  it("claims nothing without both ids", () => {
    const release = claimTurn(SESSION, "");
    expect(readKey("local", turnOwnerKey(SESSION, ""))).toBeNull();
    release.renew();
    release();
  });
});

describe("whenTurnFree", () => {
  it("fires once when the owner releases, on the storage event", () => {
    otherTabStamps();
    const onFree = vi.fn();
    whenTurnFree(SESSION, TASK, onFree);
    // A heartbeat from the owner is a storage event too, and frees nothing.
    otherTabStamps();
    window.dispatchEvent(new StorageEvent("storage", { key: KEY }));
    expect(onFree).not.toHaveBeenCalled();
    removeKey("local", KEY);
    window.dispatchEvent(new StorageEvent("storage", { key: KEY, newValue: null }));
    expect(onFree).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new StorageEvent("storage", { key: KEY, newValue: null }));
    vi.advanceTimersByTime(2 * TURN_OWNER_STALE_MS);
    expect(onFree).toHaveBeenCalledTimes(1);
  });

  it("fires when the owner's heartbeat goes stale, with no event at all", () => {
    otherTabStamps();
    const onFree = vi.fn();
    whenTurnFree(SESSION, TASK, onFree);
    vi.advanceTimersByTime(TURN_OWNER_HEARTBEAT_MS);
    expect(onFree).not.toHaveBeenCalled();
    vi.advanceTimersByTime(TURN_OWNER_STALE_MS);
    expect(onFree).toHaveBeenCalledTimes(1);
  });

  it("ignores other keys' storage events and never fires after cancel", () => {
    otherTabStamps();
    const onFree = vi.fn();
    const cancel = whenTurnFree(SESSION, TASK, onFree);
    window.dispatchEvent(new StorageEvent("storage", { key: "protoagent.ui" }));
    expect(onFree).not.toHaveBeenCalled();
    cancel();
    removeKey("local", KEY);
    window.dispatchEvent(new StorageEvent("storage", { key: KEY, newValue: null }));
    vi.advanceTimersByTime(2 * TURN_OWNER_STALE_MS);
    expect(onFree).not.toHaveBeenCalled();
  });
});
