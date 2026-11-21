// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { notifySessionChanged, startPresenceReport } from "./presenceReport";

vi.mock("../lib/api", () => ({ api: { reportPresence: vi.fn() } }));
const post = vi.mocked(api.reportPresence);
let stop: () => void;
let session: string | null;
let focused: boolean;
let visibility: DocumentVisibilityState;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(100_000);
  post.mockReset().mockResolvedValue(undefined);
  session = "chat-a";
  focused = true;
  visibility = "visible";
  vi.spyOn(document, "hasFocus").mockImplementation(() => focused);
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
  stop = startPresenceReport(() => session);
});
afterEach(() => {
  stop();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it("requires real input, then reports the selected chat every 20 seconds with a page-local id", () => {
  expect(post).toHaveBeenLastCalledWith(expect.objectContaining({ present: false, input_age_s: 61 }));
  window.dispatchEvent(new Event("pointerdown"));
  const id = post.mock.calls[post.mock.calls.length - 1][0].console_id;
  expect(id).toBeTruthy();
  expect(post).toHaveBeenLastCalledWith({ console_id: id, session: "chat-a", present: true, input_age_s: 0 });
  post.mockClear();
  vi.advanceTimersByTime(40_000);
  expect(post.mock.calls.map(([body]) => [body.present, body.input_age_s])).toEqual([[true, 20], [true, 40]]);
  stop();
  stop = startPresenceReport(() => session);
  expect(post.mock.calls[post.mock.calls.length - 1][0].console_id).toBe(id);
});

it.each(["blur", "hidden"])("reports absence once on %s and stays silent until visible and focused", (event) => {
  window.dispatchEvent(new Event("keydown"));
  post.mockClear();
  if (event === "blur") {
    focused = false;
    window.dispatchEvent(new Event("blur"));
  } else {
    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
  }
  expect(post).toHaveBeenCalledTimes(1);
  expect(post).toHaveBeenLastCalledWith(expect.objectContaining({ present: false }));
  visibility = "hidden";
  document.dispatchEvent(new Event("visibilitychange"));
  session = "chat-b";
  notifySessionChanged();
  window.dispatchEvent(new Event("pointermove"));
  vi.advanceTimersByTime(100_000);
  expect(post).toHaveBeenCalledTimes(1);
  visibility = "visible";
  focused = true;
  window.dispatchEvent(new Event("focus"));
  expect(post).toHaveBeenLastCalledWith(expect.objectContaining({ session: "chat-b", present: false }));
});

it("reports session switches immediately and clears the previous session when selection is removed", () => {
  window.dispatchEvent(new Event("keydown"));
  post.mockClear();
  session = "chat-b";
  notifySessionChanged();
  expect(post).toHaveBeenLastCalledWith(expect.objectContaining({ session: "chat-b", present: true }));
  notifySessionChanged();
  expect(post).toHaveBeenCalledTimes(1);
  session = null;
  notifySessionChanged();
  expect(post).toHaveBeenLastCalledWith(expect.objectContaining({ session: "chat-b", present: false }));
  post.mockClear();
  vi.advanceTimersByTime(100_000);
  expect(post).not.toHaveBeenCalled();
});

it.each(["pointerdown", "pointermove", "keydown", "wheel", "touchstart"])("expires idle presence and resumes immediately on %s", (event) => {
  window.dispatchEvent(new Event(event));
  vi.advanceTimersByTime(61_000);
  expect(post).toHaveBeenLastCalledWith(expect.objectContaining({ present: false, input_age_s: 60.001 }));
  post.mockClear();
  window.dispatchEvent(new Event(event));
  expect(post).toHaveBeenLastCalledWith(expect.objectContaining({ present: true, input_age_s: 0 }));
  for (let i = 0; i < 100; i++) window.dispatchEvent(new Event(event));
  expect(post).toHaveBeenCalledTimes(1);
});

it("removes listeners and timers on stop", () => {
  const removeWindow = vi.spyOn(window, "removeEventListener");
  const removeDocument = vi.spyOn(document, "removeEventListener");
  window.dispatchEvent(new Event("keydown"));
  stop();
  expect(vi.getTimerCount()).toBe(0);
  expect(removeWindow.mock.calls.map(([type]) => type)).toEqual([
    "pointerdown", "pointermove", "keydown", "wheel", "touchstart", "focus", "blur",
  ]);
  expect(removeDocument).toHaveBeenCalledWith("visibilitychange", expect.any(Function));
  post.mockClear();
  session = "chat-b";
  notifySessionChanged();
  for (const event of ["pointerdown", "pointermove", "keydown", "wheel", "touchstart", "focus", "blur"]) {
    window.dispatchEvent(new Event(event));
  }
  document.dispatchEvent(new Event("visibilitychange"));
  vi.advanceTimersByTime(100_000);
  expect(post).not.toHaveBeenCalled();
});

it("swallows failures and retries on the next heartbeat", async () => {
  post.mockRejectedValueOnce(new Error("offline"));
  window.dispatchEvent(new Event("keydown"));
  await vi.advanceTimersByTimeAsync(20_000);
  expect(post).toHaveBeenLastCalledWith(expect.objectContaining({ present: true, input_age_s: 20 }));
});
