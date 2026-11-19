import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { nativeOwnsNotices, notifyIfHidden } from "./notify";

// Presentation ownership (ADR 0135 W2): inside the desktop app the shell's native receiver
// shows hub notices, so `notifyIfHidden` must not raise a second, browser-side banner for an
// alert that mirrors one. Everything else — and every plain browser — behaves as before.

type Win = { __TAURI__?: unknown; __PROTOAGENT_API_BASE__?: string };
const win = window as unknown as Win;

class FakeNotification {
  static permission = "granted";
  static requestPermission = vi.fn(async () => "granted");
  onclick: (() => void) | null = null;
  body?: string;
  tag?: string;
  constructor(
    public title: string,
    options?: { body?: string; tag?: string },
  ) {
    this.body = options?.body;
    this.tag = options?.tag;
    created.push(this);
  }
}
const created: FakeNotification[] = [];

/** Mark this window as running inside the desktop shell, talking to the local sidecar. */
function enterDesktop(apiBase = "http://127.0.0.1:7870") {
  win.__TAURI__ = { core: { invoke: vi.fn(async () => undefined) } };
  win.__PROTOAGENT_API_BASE__ = apiBase;
}

beforeEach(() => {
  created.length = 0;
  FakeNotification.permission = "granted";
  vi.stubGlobal("Notification", FakeNotification);
  // The window is not in front — the only state in which notifyIfHidden alerts at all.
  vi.spyOn(document, "hasFocus").mockReturnValue(false);
  window.history.replaceState(null, "", "/app/");
});

afterEach(() => {
  delete win.__TAURI__;
  delete win.__PROTOAGENT_API_BASE__;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("notifyIfHidden in a plain browser", () => {
  it("raises a browser notification for a notice, exactly as for any other alert", () => {
    expect(nativeOwnsNotices()).toBe(false);
    notifyIfHidden("Agent needs a decision", "Ship it?", undefined, { notice: true });
    notifyIfHidden("Background task finished", "done");
    expect(created.map((n) => n.title)).toEqual(["Agent needs a decision", "Background task finished"]);
    expect(created[0].body).toBe("Ship it?");
  });

  it("still runs the click handler of a notice notification", () => {
    const onClick = vi.fn();
    notifyIfHidden("Agent needs a decision", "Ship it?", onClick, { notice: true });
    created[0].onclick?.();
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("stays quiet while the window is focused, flag or no flag", () => {
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    notifyIfHidden("Agent needs a decision", "Ship it?", undefined, { notice: true });
    notifyIfHidden("Background task finished");
    expect(created).toEqual([]);
  });
});

describe("notifyIfHidden inside the desktop app", () => {
  it("raises no browser notification for a notice: the native receiver shows it", () => {
    enterDesktop();
    expect(nativeOwnsNotices()).toBe(true);
    notifyIfHidden("Agent needs a decision", "Ship it?", vi.fn(), { notice: true });
    expect(created).toEqual([]);
  });

  it("does not ask for notification permission on behalf of a notice", () => {
    enterDesktop();
    FakeNotification.permission = "default";
    FakeNotification.requestPermission.mockClear();
    notifyIfHidden("Scheduled task ran", "Job j1", undefined, { notice: true });
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
  });

  it("keeps every alert that is not a notice", () => {
    enterDesktop();
    notifyIfHidden("protoAgent needs your input", "A form is waiting");
    notifyIfHidden("Background task finished", "done", vi.fn());
    notifyIfHidden("Explicitly not a notice", undefined, undefined, { notice: false });
    expect(created.map((n) => n.title)).toEqual([
      "protoAgent needs your input",
      "Background task finished",
      "Explicitly not a notice",
    ]);
  });

  it("keeps a notice from a fleet member's console: the shell only watches its own hub", () => {
    enterDesktop();
    window.history.replaceState(null, "", "/app/agent/roxy-1a2b/");
    expect(nativeOwnsNotices()).toBe(false);
    notifyIfHidden("roxy needs a decision", "Ship it?", undefined, { notice: true });
    expect(created.map((n) => n.title)).toEqual(["roxy needs a decision"]);
  });

  it("keeps a notice when the window is pointed at another machine's hub", () => {
    enterDesktop("https://hub.example.ts.net");
    expect(nativeOwnsNotices()).toBe(false);
    notifyIfHidden("Agent needs a decision", "Ship it?", undefined, { notice: true });
    expect(created).toHaveLength(1);
  });
});

it("tags a notice with the hub identity used by the worker", () => {
  notifyIfHidden("Decision", "Review", undefined, { notice: true, noticeId: "notice-123" });
  expect(created[0].tag).toBe("notice-123");
});
