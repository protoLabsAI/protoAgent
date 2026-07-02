import { describe, expect, it } from "vitest";

import { createPluginEventRelay, parseSubscribe, type RelayFrame } from "./pluginEventRelay";

// The plugin-view iframe bridge's pure relay half (#1640): subscribe parsing,
// ring-buffer replay via `since`, seq on every relayed frame, and the
// replay-then-live dedupe (never the same seq twice, nothing dropped between
// the buffer read and the live stream).

function harness(buffer: RelayFrame[] = []) {
  const posted: RelayFrame[] = [];
  const relay = createPluginEventRelay({
    post: (f) => posted.push(f),
    replaySince: (since) => buffer.filter((f) => (f.seq ?? -1) > since),
  });
  return { posted, relay };
}

describe("parseSubscribe", () => {
  it("parses a plain pre-#1640 subscribe (patterns only)", () => {
    expect(parseSubscribe({ type: "protoagent:subscribe", patterns: ["a.#", "b.*"] })).toEqual({
      patterns: ["a.#", "b.*"],
    });
  });

  it("rejects non-subscribe messages and missing/invalid patterns", () => {
    expect(parseSubscribe(null)).toBeNull();
    expect(parseSubscribe("protoagent:subscribe")).toBeNull();
    expect(parseSubscribe({ type: "protoagent:event", patterns: [] })).toBeNull();
    expect(parseSubscribe({ type: "protoagent:subscribe", patterns: "a.#" })).toBeNull();
  });

  it("filters non-string patterns (untrusted postMessage payload)", () => {
    expect(
      parseSubscribe({ type: "protoagent:subscribe", patterns: ["a.#", 7, null, "b"] }),
    ).toEqual({ patterns: ["a.#", "b"] });
  });

  it("parses since and background; drops malformed values instead of rejecting", () => {
    expect(
      parseSubscribe({ type: "protoagent:subscribe", patterns: ["a.#"], since: 42, background: true }),
    ).toEqual({ patterns: ["a.#"], since: 42, background: true });
    // Malformed optionals degrade to the plain subscribe — old behavior, not an error.
    expect(
      parseSubscribe({ type: "protoagent:subscribe", patterns: ["a.#"], since: "42", background: 1 }),
    ).toEqual({ patterns: ["a.#"] });
    expect(
      parseSubscribe({ type: "protoagent:subscribe", patterns: ["a.#"], since: Number.NaN }),
    ).toEqual({ patterns: ["a.#"] });
    // since: 0 is a real mark ("replay everything you have"), not a missing value.
    expect(
      parseSubscribe({ type: "protoagent:subscribe", patterns: ["a.#"], since: 0, background: false }),
    ).toEqual({ patterns: ["a.#"], since: 0, background: false });
  });
});

describe("live relay (no since — the pre-#1640 path, now with seq)", () => {
  it("relays matching topics with their seq, drops non-matching", () => {
    const { posted, relay } = harness();
    relay.subscribe({ patterns: ["board.#"] });
    relay.deliver("board.created", { id: "b1" }, 5);
    relay.deliver("other.created", { id: "x" }, 6);
    relay.deliver("board.moved", { id: "b1" }, 7);
    expect(posted).toEqual([
      { topic: "board.created", data: { id: "b1" }, seq: 5 },
      { topic: "board.moved", data: { id: "b1" }, seq: 7 },
    ]);
  });

  it("relays nothing before any subscribe", () => {
    const { posted, relay } = harness();
    relay.deliver("board.created", {}, 1);
    expect(posted).toEqual([]);
  });

  it("a re-subscribe replaces the pattern set", () => {
    const { posted, relay } = harness();
    relay.subscribe({ patterns: ["board.#"] });
    relay.subscribe({ patterns: ["stats.#"] });
    relay.deliver("board.created", {}, 1);
    relay.deliver("stats.tick", {}, 2);
    expect(posted).toEqual([{ topic: "stats.tick", data: {}, seq: 2 }]);
  });

  it("relays a seq-less frame as-is (no dedupe possible, nothing silently dropped)", () => {
    const { posted, relay } = harness();
    relay.subscribe({ patterns: ["#"] });
    relay.deliver("board.created", { id: "b1" });
    relay.deliver("board.created", { id: "b1" }, 3);
    expect(posted).toEqual([
      { topic: "board.created", data: { id: "b1" } },
      { topic: "board.created", data: { id: "b1" }, seq: 3 },
    ]);
  });
});

describe("replay on subscribe (since)", () => {
  const buffer: RelayFrame[] = [
    { topic: "board.created", data: { id: "b1" }, seq: 1 },
    { topic: "other.noise", data: {}, seq: 2 },
    { topic: "board.moved", data: { id: "b1" }, seq: 3 },
    { topic: "board.done", data: { id: "b1" }, seq: 4 },
  ];

  it("replays retained matching frames newer than since, in order, each with seq", () => {
    const { posted, relay } = harness(buffer);
    relay.subscribe({ patterns: ["board.#"], since: 1 });
    expect(posted).toEqual([
      { topic: "board.moved", data: { id: "b1" }, seq: 3 },
      { topic: "board.done", data: { id: "b1" }, seq: 4 },
    ]);
  });

  it("since: 0 replays the whole retained buffer (matching topics)", () => {
    const { posted, relay } = harness(buffer);
    relay.subscribe({ patterns: ["board.#"], since: 0 });
    expect(posted.map((f) => f.seq)).toEqual([1, 3, 4]);
  });

  it("no since → no replay (backward compatible)", () => {
    const { posted, relay } = harness(buffer);
    relay.subscribe({ patterns: ["board.#"] });
    expect(posted).toEqual([]);
  });

  it("live events after a replay continue without a drop or a duplicate", () => {
    const { posted, relay } = harness(buffer);
    relay.subscribe({ patterns: ["board.#"], since: 1 });
    // A frame the replay already delivered must not repeat…
    relay.deliver("board.done", { id: "b1" }, 4);
    // …and the next new one flows straight through.
    relay.deliver("board.archived", { id: "b1" }, 5);
    expect(posted.map((f) => f.seq)).toEqual([3, 4, 5]);
    expect(posted[2]).toEqual({ topic: "board.archived", data: { id: "b1" }, seq: 5 });
  });

  it("non-matching buffered frames advance nothing (their seqs stay deliverable-adjacent)", () => {
    const { posted, relay } = harness(buffer);
    relay.subscribe({ patterns: ["board.done"], since: 3 });
    expect(posted.map((f) => f.seq)).toEqual([4]);
    // seq 5 live still flows (highWater ended at 4, not confused by skipped topics).
    relay.subscribe({ patterns: ["#"] }); // widen patterns, keep the mark
    relay.deliver("other.noise", {}, 5);
    expect(posted.map((f) => f.seq)).toEqual([4, 5]);
  });

  it("re-subscribing with an older since is page-authoritative: replays again", () => {
    const { posted, relay } = harness(buffer);
    relay.subscribe({ patterns: ["board.#"], since: 3 });
    expect(posted.map((f) => f.seq)).toEqual([4]);
    // The page reloaded its model from seq 1 — it may ask for the older window again.
    relay.subscribe({ patterns: ["board.#"], since: 1 });
    expect(posted.map((f) => f.seq)).toEqual([4, 3, 4]);
  });

  it("a since past everything retained replays nothing but still gates live dupes", () => {
    const { posted, relay } = harness(buffer);
    relay.subscribe({ patterns: ["board.#"], since: 99 });
    relay.deliver("board.created", {}, 42); // stale relative to the page's own mark
    expect(posted).toEqual([]);
    relay.deliver("board.created", {}, 100);
    expect(posted.map((f) => f.seq)).toEqual([100]);
  });

  it("a plain re-subscribe (no since) keeps the dedupe mark", () => {
    const { posted, relay } = harness(buffer);
    relay.subscribe({ patterns: ["board.#"], since: 1 });
    expect(posted.map((f) => f.seq)).toEqual([3, 4]);
    relay.subscribe({ patterns: ["board.#"] }); // e.g. a background toggle re-send
    relay.deliver("board.moved", { id: "b1" }, 3); // dup of a replayed frame
    relay.deliver("board.next", {}, 5);
    expect(posted.map((f) => f.seq)).toEqual([3, 4, 5]);
  });
});
