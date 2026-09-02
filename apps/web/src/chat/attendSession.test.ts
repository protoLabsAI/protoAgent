import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Session-attendance presence client (#3110). Focus: the `?session=`/`?token=` URL, the
// sse-token auth handshake (mirrors lib/events.ts), switching the attended session, releasing
// on null, and reconnecting with a fresh token on error / after a mid-fetch session switch.

const sseToken = vi.fn(async () => ({ token: "" }) as { token: string });
vi.mock("../lib/api", () => ({
  apiUrl: (p: string) => p,
  api: {
    sseToken: () => sseToken(),
  },
}));

// Minimal EventSource fake: records the URL it was constructed with and lets a test drive
// onopen/onerror. Mirrors lib/events.test.ts.
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
}

async function loadAttend() {
  vi.resetModules();
  FakeEventSource.instances = [];
  (globalThis as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
  return import("./attendSession");
}

beforeEach(() => {
  sseToken.mockReset();
  sseToken.mockResolvedValue({ token: "" });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("buildAttendUrl", () => {
  it("always carries the session, and the token only when present", async () => {
    const { buildAttendUrl } = await loadAttend();
    expect(buildAttendUrl("/api/chat/attend", "s1", "")).toBe("/api/chat/attend?session=s1");
    expect(buildAttendUrl("/api/chat/attend", "s1", "tok")).toBe("/api/chat/attend?session=s1&token=tok");
    // URL-encodes the session id and joins with & when the base already has a query.
    expect(buildAttendUrl("/api/chat/attend?x=1", "a b", "")).toBe("/api/chat/attend?x=1&session=a+b");
  });
});

describe("attendance handshake", () => {
  it("fetches an sse-token and opens the stream with ?session=&token=", async () => {
    sseToken.mockResolvedValue({ token: "abc123" });
    const { setAttendedSession } = await loadAttend();
    setAttendedSession("sess-1");
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    expect(sseToken).toHaveBeenCalledTimes(1);
    expect(FakeEventSource.instances[0].url).toBe("/api/chat/attend?session=sess-1&token=abc123");
  });

  it("opens a tokenless stream in open mode (token \"\")", async () => {
    sseToken.mockResolvedValue({ token: "" });
    const { setAttendedSession } = await loadAttend();
    setAttendedSession("sess-1");
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    expect(FakeEventSource.instances[0].url).toBe("/api/chat/attend?session=sess-1");
  });

  it("still connects (tokenless) when the sse-token fetch rejects", async () => {
    sseToken.mockRejectedValue(new Error("401"));
    const { setAttendedSession } = await loadAttend();
    setAttendedSession("sess-1");
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    expect(FakeEventSource.instances[0].url).toBe("/api/chat/attend?session=sess-1");
  });

  it("is a no-op for a blank / whitespace session id (never registers)", async () => {
    const { setAttendedSession, attendedSessionForTest } = await loadAttend();
    setAttendedSession("   ");
    setAttendedSession("");
    setAttendedSession(null);
    // Give any stray connect() microtask a chance to run.
    await Promise.resolve();
    expect(FakeEventSource.instances.length).toBe(0);
    expect(attendedSessionForTest()).toBeNull();
  });
});

describe("switching + releasing the attended session", () => {
  it("tears down the old stream and opens the new one on a session switch", async () => {
    const { setAttendedSession, attendedSessionForTest } = await loadAttend();
    setAttendedSession("sess-1");
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const first = FakeEventSource.instances[0];
    expect(attendedSessionForTest()).toBe("sess-1");

    setAttendedSession("sess-2");
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(2));
    expect(first.closed).toBe(true); // old session released
    expect(FakeEventSource.instances[1].url).toBe("/api/chat/attend?session=sess-2");
    expect(attendedSessionForTest()).toBe("sess-2");
  });

  it("re-asserting the same session leaves the live stream untouched", async () => {
    const { setAttendedSession } = await loadAttend();
    setAttendedSession("sess-1");
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    setAttendedSession("sess-1"); // idempotent
    await Promise.resolve();
    expect(FakeEventSource.instances.length).toBe(1);
    expect(FakeEventSource.instances[0].closed).toBe(false);
  });

  it("releases the stream (fails closed to unattended) on null", async () => {
    const { setAttendedSession, attendedSessionForTest } = await loadAttend();
    setAttendedSession("sess-1");
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    setAttendedSession(null);
    expect(FakeEventSource.instances[0].closed).toBe(true);
    expect(attendedSessionForTest()).toBeNull();
  });
});

describe("reconnect + race fallback", () => {
  it("on error, refreshes the token and reconnects for the same session", async () => {
    vi.useFakeTimers();
    sseToken.mockResolvedValue({ token: "t1" });
    const { setAttendedSession } = await loadAttend();
    setAttendedSession("sess-1");

    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const first = FakeEventSource.instances[0];
    first.onopen?.({});
    expect(first.url).toBe("/api/chat/attend?session=sess-1&token=t1");

    // The socket drops; the token rotates server-side.
    sseToken.mockResolvedValue({ token: "t2" });
    first.onerror?.({});
    expect(first.closed).toBe(true);

    // Backoff timer (first attempt = 1s) → reconnect with a fresh token, same session.
    await vi.advanceTimersByTimeAsync(1000);
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(2));
    expect(FakeEventSource.instances[1].url).toBe("/api/chat/attend?session=sess-1&token=t2");
  });

  it("connects the NEW session when a switch races the token fetch", async () => {
    // Hold the first token fetch open so the switch happens mid-fetch.
    let release: (v: { token: string }) => void = () => {};
    sseToken.mockImplementationOnce(() => new Promise((res) => (release = res)));
    sseToken.mockResolvedValue({ token: "" });

    const { setAttendedSession, attendedSessionForTest } = await loadAttend();
    setAttendedSession("sess-1"); // starts fetching a token (pending)
    setAttendedSession("sess-2"); // switches while the fetch is in flight

    release({ token: "" }); // sess-1's token resolves late — must NOT open sess-1
    await vi.waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    expect(FakeEventSource.instances[0].url).toBe("/api/chat/attend?session=sess-2");
    expect(attendedSessionForTest()).toBe("sess-2");
  });
});
