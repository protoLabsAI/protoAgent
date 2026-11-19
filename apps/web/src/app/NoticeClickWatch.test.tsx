import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { useUI } from "../state/uiStore";
import { NoticeClickWatch, openNotice, takeNoticeFromUrl } from "./NoticeClickWatch";

const openWorkGroup = vi.hoisted(() => vi.fn());
vi.mock("../chat/ChatStatusRail", () => ({ openWorkGroup }));

let root: Root;
let node: HTMLDivElement;
let listeners: Array<(event: MessageEvent) => void>;

beforeEach(() => {
  openWorkGroup.mockClear();
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

it("a decision notice closes Settings and opens that decision's answer form", () => {
  useUI.getState().openGlobalSettings("devices");
  expect(openNotice("decision-dec-4ba15c14")).toBe(true);
  expect(useUI.getState().globalSettingsOpen).toBe(false);
  expect(openWorkGroup).toHaveBeenCalledWith("needs-you", "decision:dec-4ba15c14");
});

it.each([null, 7, "", "decision-", "ntc_7Qm2x", "decision-../x", "decision-" + "a".repeat(200)])(
  "leaves the operator where they are for %j", (id) => {
    useUI.getState().openGlobalSettings("devices");
    expect(openNotice(id)).toBe(false);
    expect(useUI.getState().globalSettingsOpen).toBe(true);
    expect(openWorkGroup).not.toHaveBeenCalled();
    useUI.getState().closeGlobalSettings();
  });

it("a console opened on ?notice= goes to the decision and drops the parameter", async () => {
  window.history.replaceState(null, "", "/app/?notice=decision-dec-1&flag=x#h");
  await act(async () => root.render(<NoticeClickWatch />));
  expect(openWorkGroup).toHaveBeenCalledWith("needs-you", "decision:dec-1");
  expect(window.location.pathname + window.location.search + window.location.hash).toBe("/app/?flag=x#h");
});

it("a click on an already-open console arrives from the push worker", async () => {
  await act(async () => root.render(<NoticeClickWatch />));
  expect(openWorkGroup).not.toHaveBeenCalled();
  listeners.forEach((fn) => fn({ data: { type: "something-else", id: "decision-dec-9" } } as MessageEvent));
  expect(openWorkGroup).not.toHaveBeenCalled();
  listeners.forEach((fn) => fn({ data: { type: "protoagent:notice-click", v: 1, id: "decision-dec-2", route: "/app/?notice=decision-dec-2" } } as MessageEvent));
  expect(openWorkGroup).toHaveBeenCalledWith("needs-you", "decision:dec-2");
});

it("takeNoticeFromUrl is null when there is no notice", () => {
  expect(takeNoticeFromUrl()).toBeNull();
});
