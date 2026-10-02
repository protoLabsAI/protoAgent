// useStampPoll (ADR 0112): the code pane's fallback change signal for edits nothing reported.
// Pins: the first answer is only a baseline; a moved stamp calls onChange; an unchanged one
// doesn't; a hidden tab doesn't ask; a 404 (older server / toolset off) stops the poll; an
// unmount stops it.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ fsStamp: vi.fn() }));

vi.mock("../lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("../lib/api")>();
  return { ...real, api: { ...real.api, fsStamp: mocks.fsStamp } };
});

import { ApiError } from "../lib/api";
import { STAMP_POLL_MS, STAMP_SLOW_MAX_MS } from "./liveRefresh";
import { nextStampInterval, useStampPoll } from "./useStampPoll";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLElement;
let root: Root;

function Probe({ onChange }: { onChange: () => void }) {
  useStampPoll("app", "src/a.ts", onChange);
  return null;
}

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  mocks.fsStamp.mockReset();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
});

describe("useStampPoll", () => {
  it("baseline first, then onChange only when the stamp moves", async () => {
    const stamps = ["s1", "s1", "s2", "s2"];
    mocks.fsStamp.mockImplementation(async () => ({ project: "app", is_git: true, stamp: stamps.shift() ?? "s2" }));
    const onChange = vi.fn();
    await act(async () => root.render(h(Probe, { onChange })));
    await tick(0);
    expect(mocks.fsStamp).toHaveBeenCalledWith("app", "src/a.ts");
    expect(onChange).not.toHaveBeenCalled();
    await tick(STAMP_POLL_MS); // s1 again
    expect(onChange).not.toHaveBeenCalled();
    await tick(STAMP_POLL_MS); // s2
    expect(onChange).toHaveBeenCalledTimes(1);
    await tick(STAMP_POLL_MS); // s2 again
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("backs off exponentially (to 30 s) while the server says the stamp is slow; resets on a change", async () => {
    let stamp = "s1";
    let slow = true;
    mocks.fsStamp.mockImplementation(async () => ({ project: "app", is_git: true, stamp, slow }));
    const onChange = vi.fn();
    await act(async () => root.render(h(Probe, { onChange })));
    await tick(0); // baseline (slow) → next in 4 s
    const at = () => mocks.fsStamp.mock.calls.length;
    await tick(STAMP_POLL_MS);
    expect(at()).toBe(1);
    await tick(STAMP_POLL_MS); // 4 s
    expect(at()).toBe(2);
    await tick(8_000 - 1);
    expect(at()).toBe(2);
    await tick(1); // 8 s
    expect(at()).toBe(3);
    // A change resets the cadence even while slow.
    stamp = "s2";
    await tick(16_000);
    expect(at()).toBe(4);
    expect(onChange).toHaveBeenCalledTimes(1);
    slow = false;
    await tick(STAMP_POLL_MS);
    expect(at()).toBe(5);
  });

  it("nextStampInterval caps at 30 s and resets on fast or moved", () => {
    expect(nextStampInterval(STAMP_POLL_MS, true, false)).toBe(STAMP_POLL_MS * 2);
    expect(nextStampInterval(STAMP_SLOW_MAX_MS, true, false)).toBe(STAMP_SLOW_MAX_MS);
    expect(nextStampInterval(16_000, true, false)).toBe(STAMP_SLOW_MAX_MS);
    expect(nextStampInterval(16_000, false, false)).toBe(STAMP_POLL_MS);
    expect(nextStampInterval(16_000, true, true)).toBe(STAMP_POLL_MS);
  });

  it("a 404 stops polling for good", async () => {
    mocks.fsStamp.mockRejectedValue(new ApiError(404, "disabled", "disabled"));
    await act(async () => root.render(h(Probe, { onChange: vi.fn() })));
    await tick(STAMP_POLL_MS * 5);
    expect(mocks.fsStamp).toHaveBeenCalledTimes(1);
  });

  it("does not ask while the browser tab is hidden", async () => {
    mocks.fsStamp.mockResolvedValue({ project: "app", is_git: true, stamp: "s" });
    const vis = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await act(async () => root.render(h(Probe, { onChange: vi.fn() })));
    await tick(STAMP_POLL_MS * 3);
    expect(mocks.fsStamp).not.toHaveBeenCalled();
    vis.mockReturnValue("visible");
    await tick(STAMP_POLL_MS);
    expect(mocks.fsStamp).toHaveBeenCalledTimes(1);
    vis.mockRestore();
  });

  it("unmount stops it", async () => {
    mocks.fsStamp.mockResolvedValue({ project: "app", is_git: true, stamp: "s" });
    await act(async () => root.render(h(Probe, { onChange: vi.fn() })));
    await tick(0);
    act(() => root.render(h("div")));
    const calls = mocks.fsStamp.mock.calls.length;
    await tick(STAMP_POLL_MS * 3);
    expect(mocks.fsStamp.mock.calls.length).toBe(calls);
  });
});
