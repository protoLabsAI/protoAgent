import { afterEach, beforeEach, expect, it, vi } from "vitest";

// A console that reopens resumes its event stream from a saved cursor and the server replays
// what it missed. Listeners must be able to tell that catch-up from news, or every toast
// since the last visit fires at once.

class FakeEventSource {
  static last: FakeEventSource | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  readyState = 1;
  constructor(public url: string) { FakeEventSource.last = this; }
  addEventListener(type: string, fn: (event: MessageEvent) => void) { if (type === "message") this.onmessage = fn; }
  removeEventListener() {}
  close() {}
  emit(frame: unknown) { this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent); }
}

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal("EventSource", FakeEventSource);
});
afterEach(() => { vi.unstubAllGlobals(); });

it("tells a replayed event from a live one, only while its listener runs", async () => {
  const events = await import("./events");
  const seen: Array<[string, boolean]> = [];
  const off = events.onTopic("goal.achieved", (data) => seen.push([String(data.id), events.eventIsReplay()]));
  await vi.waitFor(() => expect(FakeEventSource.last).not.toBeNull());
  const source = FakeEventSource.last!;
  const now = Date.now() / 1000;
  source.emit({ topic: "goal.achieved", data: { id: "old" }, seq: 1, ts: now - 3600 });
  source.emit({ topic: "goal.achieved", data: { id: "edge" }, seq: 2, ts: now - events.REPLAY_AGE_S - 5 });
  source.emit({ topic: "goal.achieved", data: { id: "live" }, seq: 3, ts: now - 2 });
  source.emit({ topic: "goal.achieved", data: { id: "no-ts" }, seq: 4 });
  source.emit({ topic: "goal.achieved", data: { id: "bad-ts" }, seq: 5, ts: "yesterday" });
  // The server's own reading of the age wins over this console's clock: a console whose clock
  // is an hour fast still sees a live event as live, and one an hour slow still sees an old
  // event as old.
  source.emit({ topic: "goal.achieved", data: { id: "fast-clock" }, seq: 6, ts: now - 3600, age_s: 0.4 });
  source.emit({ topic: "goal.achieved", data: { id: "slow-clock" }, seq: 7, ts: now + 3600, age_s: 7200 });
  expect(seen).toEqual([
    ["old", true], ["edge", true], ["live", false], ["no-ts", false], ["bad-ts", false],
    ["fast-clock", false], ["slow-clock", true],
  ]);
  // Outside a dispatch nothing is a replay.
  expect(events.eventIsReplay()).toBe(false);
  off();
});
