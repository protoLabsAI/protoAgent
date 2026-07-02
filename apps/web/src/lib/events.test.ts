import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The SSE event-bus client (ADR 0003/0039). Focus: topic matching, the `?token=`
// auth handshake (#873 — the server requires an sse-token once a bearer is set),
// and `?since=` replay continuity on reconnect.

// apiUrl is identity-ish for "/api/events"; sseToken is mocked per-test.
const sseToken = vi.fn(async () => ({ token: "" }) as { token: string });
vi.mock("./api", () => ({
  apiUrl: (p: string) => p,
  api: {
    sseToken: () => sseToken(),
  },
}));

// Minimal EventSource fake: records the URL it was constructed with and lets a
// test drive onopen/onmessage/onerror.
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onopen: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: unknown) => void) | null = null;
  closed = false;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  close() {
    this.closed = true;
  }
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

// Re-import the module fresh each test so its module-level singleton state resets.
async function loadEvents() {
  vi.resetModules();
  FakeEventSource.instances = [];
  (globalThis as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
  return import("./events");
}

beforeEach(() => {
  sseToken.mockReset();
  sseToken.mockResolvedValue({ token: "" });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("topicMatches", () => {
  it("matches exact, single-segment *, and tail #", async () => {
    const { topicMatches } = await loadEvents();
    expect(topicMatches("a.b", "a.b")).toBe(true);
    expect(topicMatches("a.*", "a.b")).toBe(true);
    expect(topicMatches("a.*", "a.b.c")).toBe(false);
    expect(topicMatches("a.#", "a.b.c")).toBe(true);
    expect(topicMatches("#", "anything.here")).toBe(true);
    expect(topicMatches("a.b", "a.c")).toBe(false);
  });
});

describe("buildEventsUrl", () => {
  it("returns the bare base when no token and no since", async () => {
    const { buildEventsUrl } = await loadEvents();
    expect(buildEventsUrl("/api/events", "", null)).toBe("/api/events");
  });

  it("appends token and since, joining with ? or &", async () => {
    const { buildEventsUrl } = await loadEvents();
    expect(buildEventsUrl("/api/events", "tok", null)).toBe("/api/events?token=tok");
    expect(buildEventsUrl("/api/events", "", 7)).toBe("/api/events?since=7");
    expect(buildEventsUrl("/api/events", "tok", 7)).toBe("/api/events?token=tok&since=7");
    expect(buildEventsUrl("/api/events?x=1", "tok", null)).toBe("/api/events?x=1&token=tok");
  });
});

describe("connect handshake", () => {
  it("fetches an sse-token and opens EventSource with ?token=", async () => {
    sseToken.mockResolvedValue({ token: "abc123" });
    const { onTopic } = await loadEvents();
    onTopic("x.*", () => {});
    // connect() awaits the token fetch microtask before constructing EventSource.
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    expect(sseToken).toHaveBeenCalledTimes(1);
    expect(FakeEventSource.instances[0].url).toBe("/api/events?token=abc123");
  });

  it("opens a tokenless stream in open mode (token \"\")", async () => {
    sseToken.mockResolvedValue({ token: "" });
    const { onTopic } = await loadEvents();
    onTopic("#", () => {});
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    expect(FakeEventSource.instances[0].url).toBe("/api/events");
  });

  it("still connects (tokenless) when the sse-token fetch rejects", async () => {
    sseToken.mockRejectedValue(new Error("401"));
    const { onTopic } = await loadEvents();
    onTopic("#", () => {});
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    expect(FakeEventSource.instances[0].url).toBe("/api/events");
  });
});

describe("client ring buffer + seq (#1640 — plugin-bridge replay)", () => {
  it("hands listeners the frame's seq as a third argument", async () => {
    const { onTopic } = await loadEvents();
    const seen: Array<number | undefined> = [];
    onTopic("job.*", (_data, _topic, seq) => seen.push(seq));
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const es = FakeEventSource.instances[0];
    es.emit({ topic: "job.start", data: {}, seq: 7 });
    es.emit({ topic: "job.end", data: {} }); // no seq → undefined, still delivered
    expect(seen).toEqual([7, undefined]);
  });

  it("retains seq'd frames and replays only those newer than since, in order", async () => {
    const { onTopic, replaySince } = await loadEvents();
    onTopic("#", () => {});
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const es = FakeEventSource.instances[0];
    es.emit({ topic: "a.one", data: { n: 1 }, seq: 1 });
    es.emit({ topic: "b.two", data: { n: 2 }, seq: 2 });
    es.emit({ topic: "a.three", data: { n: 3 } }); // seq-less → NOT retained (unreplayable)
    es.emit({ topic: "a.four", data: { n: 4 }, seq: 4 });
    expect(replaySince(1)).toEqual([
      { topic: "b.two", data: { n: 2 }, seq: 2 },
      { topic: "a.four", data: { n: 4 }, seq: 4 },
    ]);
    expect(replaySince(4)).toEqual([]);
    expect(replaySince(0).map((f) => f.seq)).toEqual([1, 2, 4]);
  });

  it("caps retention (drop-oldest) so replay is best-effort like the server ring", async () => {
    const { onTopic, replaySince } = await loadEvents();
    onTopic("#", () => {});
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const es = FakeEventSource.instances[0];
    for (let i = 1; i <= 300; i++) es.emit({ topic: "t.x", data: {}, seq: i });
    const frames = replaySince(0);
    expect(frames.length).toBe(256); // RING_MAX
    expect(frames[0].seq).toBe(300 - 256 + 1); // oldest were dropped
    expect(frames[frames.length - 1].seq).toBe(300);
  });
});

describe("reconnect", () => {
  it("on error, refreshes the token and replays via ?since=<lastSeq>", async () => {
    vi.useFakeTimers();
    sseToken.mockResolvedValue({ token: "t1" });
    const seen: number[] = [];
    const { onTopic } = await loadEvents();
    onTopic("job.*", (data) => seen.push((data as { n: number }).n));

    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const first = FakeEventSource.instances[0];
    first.onopen?.({});
    // Deliver an event carrying seq=42 so the next connect should resume after it.
    first.emit({ topic: "job.start", data: { n: 1 }, seq: 42 });
    expect(seen).toEqual([1]);

    // Drop the connection; the token rotates server-side.
    sseToken.mockResolvedValue({ token: "t2" });
    first.onerror?.({});
    expect(first.closed).toBe(true);

    // Backoff timer (first attempt = 1s) → reconnect with fresh token + since.
    await vi.advanceTimersByTimeAsync(1000);
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(2));
    expect(FakeEventSource.instances[1].url).toBe("/api/events?token=t2&since=42");
  });
});
