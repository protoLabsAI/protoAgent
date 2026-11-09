import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { __resetStorageSeamForTests, readKey, storagePressure, writeKey } from "./storage";
import {
  LOCAL_BUDGET_BYTES,
  RECENT_WRITE_MS,
  enforceStorageBudget,
  evict,
  evictionCandidates,
  installStorageEviction,
  lastWrittenAt,
  setFleetSlugs,
  subscribeEvictions,
} from "./storageEviction";
import { QuotaStorage } from "./quotaStorage.testkit";

// ADR 0114 D4/D5 (#4972): old agents' saved chats are cleared before the quota fills —
// never this window's agent, the host, anything written in the last day, or a non-transcript.

const NOW = 1_800_000_000_000;
const DAY = RECENT_WRITE_MS;
let local: QuotaStorage;

const chats = (updatedAt: number, pad = 0) =>
  JSON.stringify({ version: 1, sessions: [{ id: "chat-1", messages: [], updatedAt, pad: "x".repeat(pad) }] });

function seed(entries: Record<string, string>) {
  for (const [key, value] of Object.entries(entries)) local.setItem(key, value);
}

beforeEach(() => {
  __resetStorageSeamForTests();
  local = new QuotaStorage(10_000_000);
  vi.stubGlobal("localStorage", local);
  vi.stubGlobal("sessionStorage", new QuotaStorage(10_000));
  window.history.replaceState(null, "", "/app/agent/here-1/");
  setFleetSlugs(["host", "here-1", "quiet-2", "busy-3"]);
});

afterEach(() => {
  setFleetSlugs(null);
  vi.unstubAllGlobals();
  __resetStorageSeamForTests();
  window.history.replaceState(null, "", "/");
});

describe("which saved chats eviction may clear", () => {
  it("reads a chat blob's newest write; a thread without times is unknown", () => {
    expect(lastWrittenAt(JSON.stringify({ sessions: [{ updatedAt: 5 }, { updatedAt: 9 }, {}] }))).toBe(9);
    expect(lastWrittenAt(JSON.stringify({ contextId: "c", messages: [] }))).toBeNull();
    expect(lastWrittenAt("not json")).toBeNull();
    expect(lastWrittenAt(null)).toBeNull();
  });

  it("orders agents gone from the fleet first, then quiet fleet agents; spares everything live", () => {
    seed({
      "protoagent.chat.sessions": chats(NOW - 30 * DAY), // the host: never
      "protoagent.chat.sessions:here-1": chats(NOW - 30 * DAY), // this window: never
      "protoagent.chat.sessions:busy-3": chats(NOW - 60_000), // written in the last day: never
      "protoagent.chat.sessions:quiet-2": chats(NOW - 3 * DAY), // quiet fleet agent: tier 2
      "protoagent.chat.sessions:gone-4": chats(NOW - 2 * DAY), // left the fleet: tier 1
      "protoagent.chat.sessions:gone-5": chats(NOW - 9 * DAY), // left the fleet, older: tier 1 first
      "protoagent.palette.chat:gone-4:dm:x": JSON.stringify({ contextId: "c", messages: [] }), // no time, gone agent: tier 1
      "protoagent.palette.chat:quiet-2:dm:x": JSON.stringify({ contextId: "c", messages: [] }), // no time, fleet agent: never
      "protoagent.chat.sessions:gone-4.dismissed": "[]", // a dismissal set, not a transcript: never
      "protoagent.ui:gone-4": "{}", // layout: not this step
      "protoagent.authToken": "secret",
    });
    expect(evictionCandidates({ now: NOW }).map((c) => [c.key, c.tier])).toEqual([
      ["protoagent.palette.chat:gone-4:dm:x", 1], // unknown time sorts as oldest
      ["protoagent.chat.sessions:gone-5", 1],
      ["protoagent.chat.sessions:gone-4", 1],
      ["protoagent.chat.sessions:quiet-2", 2],
    ]);
  });

  it("with the fleet unknown, only a known-quiet chat blob is a candidate", () => {
    setFleetSlugs(null);
    seed({
      "protoagent.chat.sessions:gone-4": chats(NOW - 2 * DAY),
      "protoagent.chat.sessions:busy-3": chats(NOW - 60_000),
      "protoagent.palette.chat:gone-4:dm:x": JSON.stringify({ contextId: "c", messages: [] }),
    });
    expect(evictionCandidates({ now: NOW }).map((c) => [c.key, c.tier])).toEqual([["protoagent.chat.sessions:gone-4", 2]]);
  });

  it("stops once enough is freed and tells listeners what it cleared", () => {
    seed({
      "protoagent.chat.sessions:gone-5": chats(NOW - 9 * DAY, 1000),
      "protoagent.chat.sessions:gone-4": chats(NOW - 2 * DAY, 1000),
    });
    const heard = vi.fn();
    const stop = subscribeEvictions(heard);
    const report = evict(100, { reason: "quota", now: NOW });
    stop();
    expect(report.evicted.map((e) => e.key)).toEqual(["protoagent.chat.sessions:gone-5"]);
    expect(readKey("local", "protoagent.chat.sessions:gone-5")).toBeNull();
    expect(readKey("local", "protoagent.chat.sessions:gone-4")).not.toBeNull();
    expect(heard).toHaveBeenCalledWith(report);
  });
});

describe("when eviction runs", () => {
  it("the budget check does nothing under budget, and clears down to the target over it", () => {
    seed({ "protoagent.chat.sessions:gone-4": chats(NOW - 2 * DAY, 1000) });
    expect(enforceStorageBudget({ now: NOW }).evicted).toEqual([]);
    expect(enforceStorageBudget({ now: NOW, usage: LOCAL_BUDGET_BYTES + 1 }).evicted.map((e) => e.key)).toEqual([
      "protoagent.chat.sessions:gone-4",
    ]);
  });

  it("a write that hits the quota clears an old agent's chats and then saves", () => {
    local = new QuotaStorage(6_000);
    vi.stubGlobal("localStorage", local);
    local.setItem("protoagent.chat.sessions:gone-4", chats(NOW - 2 * DAY, 2000));
    const uninstall = installStorageEviction();
    try {
      vi.spyOn(Date, "now").mockReturnValue(NOW);
      // This window's own chats need the room the old agent's saved chats are using.
      const result = writeKey("local", "protoagent.chat.sessions:here-1", chats(NOW, 1500));
      expect(result).toEqual({ ok: true });
      expect(readKey("local", "protoagent.chat.sessions:gone-4")).toBeNull();
      expect(storagePressure().state).toBe("ok");
    } finally {
      uninstall();
    }
  });

  it("never clears the key the failing write is saving", () => {
    seed({ "protoagent.chat.sessions:gone-4": chats(NOW - 2 * DAY) });
    expect(evict(1, { reason: "quota", now: NOW, exclude: "protoagent.chat.sessions:gone-4" }).evicted).toEqual([]);
  });
});
