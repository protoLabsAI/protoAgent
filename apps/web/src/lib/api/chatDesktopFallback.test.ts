// The desktop shell streams a turn through the native `chat_stream` command and falls back
// to the non-streaming /api/chat path when that command is unavailable or fails (#4975).
// The fallback re-sends the message, so it is only safe while the server has not taken the
// turn yet. Once the native stream delivered anything, the turn is running server-side and
// a re-send runs it a second time: the agent answers one message twice.
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const native = vi.hoisted(() => ({
  invoke: vi.fn<(cmd: string, args: { onEvent: { onmessage: (m: string) => void } }) => Promise<void>>(),
}));

vi.mock("./desktop", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./desktop")>();
  class Channel<T> {
    onmessage: (msg: T) => void = () => {};
  }
  return {
    ...actual,
    isDesktopWebview: () => true,
    tauriCore: () => ({ Channel, invoke: native.invoke }),
  };
});

// The setup file preloads modules that import the api before this file's mock registers,
// so load a fresh copy that sees the mocked shell.
let api: typeof import("../api").api;
beforeAll(async () => {
  vi.resetModules();
  ({ api } = await import("../api"));
});

const fetchMock = vi.fn(
  async (_url: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify({ response: "fallback answer" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
);

beforeEach(() => {
  native.invoke.mockReset();
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const SSE_TASK =
  'data: {"jsonrpc":"2.0","id":"r","result":{"task":{"id":"07aa9df5","contextId":"chat-1","status":{"state":"TASK_STATE_WORKING"}}}}\n\n';

function chatPosts() {
  return fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/chat"));
}

describe("desktop chat stream fallback (#4975)", () => {
  it("does not re-send the message over /api/chat once the native stream delivered frames", async () => {
    native.invoke.mockImplementation(async (_cmd, args) => {
      args.onEvent.onmessage(SSE_TASK); // the server accepted the message and started the turn
      throw new Error("error decoding response body");
    });
    const onText = vi.fn();

    await expect(api.streamChat("twice daily", "chat-1", { onText })).rejects.toThrow(/decoding/);

    expect(native.invoke).toHaveBeenCalledTimes(1);
    expect(chatPosts()).toHaveLength(0);
    expect(onText).not.toHaveBeenCalledWith("fallback answer", false);
  });

  it("does not re-send a message the shell refused as already sent", async () => {
    // Tauri invoke rejects with the command's error STRING, not an Error.
    native.invoke.mockRejectedValue("chat_stream: already sent: message web-1-abc");

    await expect(api.streamChat("twice daily", "chat-1", { onText: vi.fn() })).rejects.toBe(
      "chat_stream: already sent: message web-1-abc",
    );
    expect(chatPosts()).toHaveLength(0);
  });

  it("still falls back when the native command failed before the server took the turn", async () => {
    native.invoke.mockRejectedValue(new Error("command chat_stream not found"));
    const onText = vi.fn();

    await api.streamChat("twice daily", "chat-1", { onText });

    expect(chatPosts()).toHaveLength(1);
    expect(onText).toHaveBeenCalledWith("fallback answer", false);
  });
});
