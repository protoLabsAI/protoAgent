import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { agentHref } from "../lib/api/routing";
import { useUI } from "../state/uiStore";
import { NoticeClickWatch, openNotice, takeClickedNotice, takeNoticeFromUrl } from "./NoticeClickWatch";

const openWorkGroup = vi.hoisted(() => vi.fn());
vi.mock("../chat/ChatStatusRail", () => ({ openWorkGroup }));

let root: Root;
let node: HTMLDivElement;
let listeners: Array<(event: MessageEvent) => void>;
let origin: string | null;
const fetchMock = vi.fn(async (_url: string) => new Response(JSON.stringify({ ok: true, agent: origin })));
const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
const nextLook = () => act(async () => { await new Promise((r) => setTimeout(r, 500)); });

beforeEach(() => {
  openWorkGroup.mockClear();
  fetchMock.mockClear();
  origin = null;
  vi.stubGlobal("fetch", fetchMock);
  listeners = [];
  vi.stubGlobal("navigator", { serviceWorker: {
    addEventListener: (_: string, fn: (event: MessageEvent) => void) => listeners.push(fn),
    removeEventListener: (_: string, fn: (event: MessageEvent) => void) => { listeners = listeners.filter((l) => l !== fn); },
  } });
  window.history.replaceState(null, "", "/app/");
  node = document.createElement("div");
  document.body.append(node);
  root = createRoot(node);
});
afterEach(async () => {
  await act(async () => root.unmount());
  node.remove();
  vi.unstubAllGlobals();
});

it("a decision notice closes Settings and opens that decision's answer form", async () => {
  useUI.getState().openGlobalSettings("devices");
  expect(await openNotice("decision-dec-4ba15c14")).toBe(true);
  expect(String(fetchMock.mock.calls[0][0])).toContain("/api/notices/decision-dec-4ba15c14");
  expect(useUI.getState().globalSettingsOpen).toBe(false);
  expect(openWorkGroup).toHaveBeenCalledWith("needs-you", "decision:dec-4ba15c14");
});

it.each([null, 7, "", "decision-", "ntc_7Qm2x", "decision-../x", "decision-" + "a".repeat(200)])(
  "leaves the operator where they are for %j", async (id) => {
    useUI.getState().openGlobalSettings("devices");
    expect(await openNotice(id)).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(useUI.getState().globalSettingsOpen).toBe(true);
    expect(openWorkGroup).not.toHaveBeenCalled();
    useUI.getState().closeGlobalSettings();
  });

it("a console opened on ?notice= goes to the decision and drops the parameter", async () => {
  window.history.replaceState(null, "", "/app/?notice=decision-dec-1&flag=x#h");
  await act(async () => root.render(<NoticeClickWatch />));
  await flush();
  expect(openWorkGroup).toHaveBeenCalledWith("needs-you", "decision:dec-1");
  expect(window.location.pathname + window.location.search + window.location.hash).toBe("/app/?flag=x#h");
});

it("a click on an already-open console arrives from the push worker", async () => {
  await act(async () => root.render(<NoticeClickWatch />));
  expect(openWorkGroup).not.toHaveBeenCalled();
  listeners.forEach((fn) => fn({ data: { type: "something-else", id: "decision-dec-9" } } as MessageEvent));
  expect(openWorkGroup).not.toHaveBeenCalled();
  listeners.forEach((fn) => fn({ data: { type: "protoagent:notice-click", v: 1, id: "decision-dec-2", route: "/app/?notice=decision-dec-2" } } as MessageEvent));
  await flush();
  expect(openWorkGroup).toHaveBeenCalledWith("needs-you", "decision:dec-2");
});

it("takeNoticeFromUrl is null when there is no notice", () => {
  expect(takeNoticeFromUrl()).toBeNull();
});

it("a decision another agent asked for opens in that agent's console", async () => {
  origin = "protoengineer-ba4c";
  const go = vi.fn();
  useUI.getState().openGlobalSettings("devices");
  expect(await openNotice("decision-dec-7", go)).toBe(true);
  expect(go).toHaveBeenCalledWith(`${agentHref("protoengineer-ba4c")}?notice=decision-dec-7`);
  expect(openWorkGroup).not.toHaveBeenCalled(); // the other console opens it
});

it("once in that agent's console, the decision opens there", async () => {
  origin = "protoengineer-ba4c";
  window.history.replaceState(null, "", "/app/agent/protoengineer-ba4c/");
  const go = vi.fn();
  expect(await openNotice("decision-dec-7", go)).toBe(true);
  expect(go).not.toHaveBeenCalled();
  expect(openWorkGroup).toHaveBeenCalledWith("needs-you", "decision:dec-7");
});

it("opens the decision here when the hub can't say who raised it", async () => {
  fetchMock.mockRejectedValueOnce(new Error("offline"));
  expect(await openNotice("decision-dec-8")).toBe(true);
  expect(openWorkGroup).toHaveBeenCalledWith("needs-you", "decision:dec-8");
});

it("the latest click wins when an earlier lookup answers late", async () => {
  let answerFirst!: (r: Response) => void;
  fetchMock.mockReturnValueOnce(new Promise<Response>((resolve) => { answerFirst = resolve; }));
  const first = openNotice("decision-dec-a");
  expect(await openNotice("decision-dec-b")).toBe(true);
  answerFirst(new Response(JSON.stringify({ ok: true, agent: "someone-else" })));
  expect(await first).toBe(false);
  expect(openWorkGroup).toHaveBeenCalledTimes(1);
  expect(openWorkGroup).toHaveBeenCalledWith("needs-you", "decision:dec-b");
});

// ── the click the push worker left behind (iOS: no URL, no message) ──────────────────
function stubClickCache(entry: unknown | null) {
  let stored: unknown | null = entry;
  const cache = { match: vi.fn(async () => (stored === null ? undefined : new Response(JSON.stringify(stored)))) };
  const storage = {
    has: vi.fn(async () => stored !== null),
    open: vi.fn(async () => cache),
    delete: vi.fn(async () => { stored = null; return true; }),
  };
  vi.stubGlobal("caches", storage);
  return storage;
}

it("takes a recent click once, then it is gone", async () => {
  const cache = stubClickCache({ v: 1, id: "decision-dec-5", at: 1_000 });
  expect(await takeClickedNotice(1_500)).toBe("decision-dec-5");
  expect(cache.delete).toHaveBeenCalledOnce();
  expect(await takeClickedNotice(1_600)).toBeNull();
});

it.each([
  ["stale", { v: 1, id: "decision-dec-5", at: 0 }, 6 * 60_000],
  ["from the future", { v: 1, id: "decision-dec-5", at: 9_000 }, 1_000],
  ["malformed", { v: 1, id: 5 }, 1_000],
])("ignores a %s click and still clears it", async (_name, entry, now) => {
  const cache = stubClickCache(entry);
  expect(await takeClickedNotice(now)).toBeNull();
  expect(cache.delete).toHaveBeenCalledOnce();
});

it("looking for a click creates nothing", async () => {
  const storage = stubClickCache(null);
  expect(await takeClickedNotice()).toBeNull();
  expect(storage.open).not.toHaveBeenCalled();
});

it("is quiet without Cache Storage", async () => {
  vi.stubGlobal("caches", undefined);
  expect(await takeClickedNotice()).toBeNull();
});

it("coming to the front opens the decision the worker remembered", async () => {
  stubClickCache(null);
  await act(async () => root.render(<NoticeClickWatch />));
  await flush();
  expect(openWorkGroup).not.toHaveBeenCalled();
  // The note lands AFTER the console came to the front (the worker handled the tap late):
  // no new event fires, and the console must still find it.
  stubClickCache({ v: 1, id: "decision-dec-6", at: Date.now() });
  await nextLook();
  await flush();
  expect(openWorkGroup).toHaveBeenCalledWith("needs-you", "decision:dec-6");
});

it("a click that arrives by message spends the worker's note, so a later focus doesn't reopen it", async () => {
  stubClickCache(null);
  await act(async () => root.render(<NoticeClickWatch />));
  await flush();
  const storage = stubClickCache({ v: 1, id: "decision-dec-3", at: Date.now() });
  listeners.forEach((fn) => fn({ data: { type: "protoagent:notice-click", v: 1, id: "decision-dec-3", route: "/app/" } } as MessageEvent));
  await flush();
  await flush();
  expect(storage.delete).toHaveBeenCalledOnce();
  expect(openWorkGroup).toHaveBeenCalledTimes(1);
  await act(async () => { window.dispatchEvent(new Event("focus")); });
  await nextLook();
  expect(openWorkGroup).toHaveBeenCalledTimes(1);
});

it("records the console's half of a tap for Settings", async () => {
  localStorage.removeItem("protoagent.noticeTapLog");
  stubClickCache({ v: 1, id: "decision-dec-9", at: Date.now() });
  await act(async () => root.render(<NoticeClickWatch />));
  await flush();
  await flush();
  const steps = (JSON.parse(localStorage.getItem("protoagent.noticeTapLog") || "[]") as Array<{ step: string; detail: string }>)
    .map((r) => `${r.step}: ${r.detail}`);
  expect(steps[0]).toContain("start: /app/"); // the routine line is kept because a tap followed
  expect(steps).toContain("note: found decision-dec-9");
  expect(steps.some((line) => line.startsWith("open: decision-dec-9 raised by this agent"))).toBe(true);
});

it("a launch with no tap leaves no log", async () => {
  localStorage.removeItem("protoagent.noticeTapLog");
  stubClickCache(null);
  await act(async () => root.render(<NoticeClickWatch />));
  await flush();
  await act(async () => { window.dispatchEvent(new Event("focus")); });
  await nextLook();
  expect(JSON.parse(localStorage.getItem("protoagent.noticeTapLog") || "[]")).toEqual([]);
});
