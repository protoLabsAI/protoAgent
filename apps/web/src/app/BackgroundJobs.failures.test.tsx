// The background agents dialog says so when an action fails (#4733 QA). Copy used to
// leak an unhandled rejection, and stop/delete/clear updated the list optimistically and
// swallowed the API error, so a refused action looked done. Mounted for real; only the
// API, the event bus and the toast are stubbed.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BackgroundJobDTO } from "../lib/types";

type Toast = { tone?: string; title: string; message?: unknown };
const mocks = vi.hoisted(() => ({
  toasts: [] as Toast[],
  background: vi.fn(),
  stopBackground: vi.fn(),
  deleteBackground: vi.fn(),
  clearFinishedBackground: vi.fn(),
}));
vi.mock("@protolabsai/ui/overlays", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useToast: () => (t: Toast) => {
    mocks.toasts.push(t);
    return "t1";
  },
}));
vi.mock("../lib/api", () => ({
  api: {
    background: mocks.background,
    stopBackground: mocks.stopBackground,
    deleteBackground: mocks.deleteBackground,
    clearFinishedBackground: mocks.clearFinishedBackground,
  },
}));
vi.mock("../lib/events", () => ({
  onTopic: () => () => {},
  onConnectionChange: () => () => {},
}));

import { useUI } from "../state/uiStore";
import { BackgroundJobs, CopyResult } from "./BackgroundJobs";
import { restoreRemoved, revertCancel } from "./background-jobs";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function job(p: Partial<BackgroundJobDTO>): BackgroundJobDTO {
  return { id: "x", status: "completed", subagent_type: "researcher", description: "d", ...p };
}

const RUNNING = job({ id: "run1", status: "running", description: "still going", created_at: "2026-11-04T00:00:00Z" });
const DONE = job({ id: "done1", status: "completed", description: "all done", result: "the report" });

function stubClipboard(value: unknown) {
  Object.defineProperty(navigator, "clipboard", { value, configurable: true });
}

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  window.localStorage.clear();
  mocks.toasts.length = 0;
  for (const f of [mocks.background, mocks.stopBackground, mocks.deleteBackground, mocks.clearFinishedBackground]) {
    f.mockReset();
  }
  mocks.background.mockResolvedValue({ enabled: true, jobs: [RUNNING, DONE] });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.body.innerHTML = "";
  stubClipboard(undefined);
});

async function openDialog() {
  await act(async () => root.render(h(BackgroundJobs)));
  await act(async () => useUI.getState().openBackgroundJobs());
}

function button(match: (b: HTMLButtonElement) => boolean): HTMLButtonElement {
  const b = [...document.querySelectorAll<HTMLButtonElement>("button")].find(match);
  if (!b) throw new Error("button not found");
  return b;
}

const bodyText = () => document.body.textContent || "";

describe("BackgroundJobs optimistic actions", () => {
  it("rolls back a refused stop and says so", async () => {
    mocks.stopBackground.mockRejectedValue(new Error("502 Bad Gateway"));
    await openDialog();
    const stop = button((b) => /stop/i.test(b.getAttribute("aria-label") || ""));
    await act(async () => stop.click());
    expect(mocks.stopBackground).toHaveBeenCalledWith("run1");
    expect(mocks.toasts).toEqual([
      { tone: "error", title: "Couldn't stop the background agent", message: "502 Bad Gateway" },
    ]);
    // Still running: the Stop control is back, not a canceled row.
    expect(button((b) => /stop/i.test(b.getAttribute("aria-label") || ""))).toBeTruthy();
  });

  it("restores a refused delete and says so", async () => {
    mocks.deleteBackground.mockRejectedValue(new Error("nope"));
    await openDialog();
    expect(bodyText()).toContain("all done");
    const del = button((b) => /(delete|remove)/i.test(b.getAttribute("aria-label") || ""));
    await act(async () => del.click());
    expect(mocks.deleteBackground).toHaveBeenCalledWith("done1");
    expect(bodyText()).toContain("all done");
    expect(mocks.toasts[0]).toMatchObject({ tone: "error", title: "Couldn't remove the background job" });
  });

  it("restores a refused clear-finished and says so", async () => {
    mocks.clearFinishedBackground.mockRejectedValue(new Error("nope"));
    await openDialog();
    await act(async () => button((b) => /clear finished/i.test(b.textContent || "")).click());
    expect(mocks.clearFinishedBackground).toHaveBeenCalled();
    expect(bodyText()).toContain("all done");
    expect(mocks.toasts[0]).toMatchObject({ tone: "error", title: "Couldn't clear finished jobs" });
  });

  it("stays quiet when the server accepts", async () => {
    mocks.deleteBackground.mockResolvedValue(undefined);
    await openDialog();
    await act(async () => button((b) => /(delete|remove)/i.test(b.getAttribute("aria-label") || "")).click());
    expect(bodyText()).not.toContain("all done");
    expect(mocks.toasts).toEqual([]);
  });
});

describe("CopyResult", () => {
  async function renderAndClick() {
    await act(async () => root.render(h(CopyResult, { text: "the report" })));
    const btn = container.querySelector("button")!;
    await act(async () => btn.click());
    return btn;
  }

  it("says Copied once the write resolves", async () => {
    stubClipboard({ writeText: vi.fn().mockResolvedValue(undefined) });
    const btn = await renderAndClick();
    expect(btn.textContent).toContain("Copied");
  });

  it("says Copy failed when the write rejects, without an unhandled rejection", async () => {
    stubClipboard({ writeText: vi.fn().mockRejectedValue(new Error("denied")) });
    const btn = await renderAndClick();
    expect(btn.textContent).toContain("Copy failed");
  });

  it("says Copy failed when the Clipboard API is missing", async () => {
    stubClipboard(undefined);
    const btn = await renderAndClick();
    expect(btn.textContent).toContain("Copy failed");
  });
});

describe("rollback helpers", () => {
  it("revertCancel restores the prior status only while the row still reads canceled", () => {
    const canceled = { a: job({ id: "a", status: "canceled" }) };
    expect(revertCancel(canceled, "a", "running").a.status).toBe("running");
    const completed = { a: job({ id: "a", status: "completed" }) };
    expect(revertCancel(completed, "a", "running")).toBe(completed);
    expect(revertCancel({}, "a", "running")).toEqual({});
  });

  it("restoreRemoved re-adds missing jobs and never overwrites a fresher copy", () => {
    const fresher = job({ id: "a", status: "completed", result: "new" });
    const out = restoreRemoved({ a: fresher }, { a: job({ id: "a", result: "old" }), b: job({ id: "b" }) });
    expect(out.a).toBe(fresher);
    expect(out.b?.id).toBe("b");
    const same = { a: fresher };
    expect(restoreRemoved(same, { a: job({ id: "a" }) })).toBe(same);
  });
});
