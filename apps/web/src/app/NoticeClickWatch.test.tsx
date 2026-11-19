import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { agentHref } from "../lib/api/routing";
import { useUI } from "../state/uiStore";
import { NoticeClickWatch, openNotice, takeNoticeFromUrl } from "./NoticeClickWatch";

const openWorkGroup = vi.hoisted(() => vi.fn());
vi.mock("../chat/ChatStatusRail", () => ({ openWorkGroup }));

let root: Root;
let node: HTMLDivElement;
let listeners: Array<(event: MessageEvent) => void>;
let origin: string | null;
const fetchMock = vi.fn(async (_url: string) => new Response(JSON.stringify({ ok: true, agent: origin })));
const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

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
