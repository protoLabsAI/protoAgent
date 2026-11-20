import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { chatStore } from "../chat/chat-store";
import { openDeepLink, snapshotForTest } from "../chat/conversations-store";
import { HomeAlertCard } from "../chat/HomeAlertCard";
import { homeQueryKey } from "../chat/homeTab";
import { agentHref } from "../lib/api/routing";
import { queryClient } from "../lib/queryClient";
import type { ChatMessage } from "../lib/types";
import { useUI } from "../state/uiStore";
import { NoticeClickWatch, openClickedNotice, openNotice, takeClickedNotice, takeNoticeFromUrl } from "./NoticeClickWatch";

const openWorkGroup = vi.hoisted(() => vi.fn());
vi.mock("../chat/ChatStatusRail", () => ({ openWorkGroup }));

let root: Root;
let node: HTMLDivElement;
let listeners: Array<(event: MessageEvent) => void>;
let origin: string | null;
// What `GET /api/home/entries` answers; null is a console with Home off (404).
let homeRead: unknown | null;
const fetchMock = vi.fn(async (url: string) => String(url).includes("/api/home/entries")
  ? new Response(JSON.stringify(homeRead ?? { detail: "Not Found" }), { status: homeRead ? 200 : 404 })
  : new Response(JSON.stringify({ ok: true, agent: origin })));
const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });
const nextLook = () => act(async () => { await new Promise((r) => setTimeout(r, 500)); });

beforeEach(() => {
  openWorkGroup.mockClear();
  fetchMock.mockClear();
  origin = null;
  homeRead = null;
  queryClient.removeQueries({ queryKey: homeQueryKey() });
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

// ── the notice's entry in Home (#5545) ────────────────────────────────────────────────
const scrolled = vi.fn();
let homeCount = 0;

function homeEntry(noticeId: string, source = "fleet-health"): ChatMessage {
  return {
    id: `home-entry-${noticeId}`, role: "user", status: "done", createdAt: 1_000,
    content: `[Home entry: notify]\nprotoEngineer stopped\n\nIt exited with code 1.`,
    home: { kind: source === "decision" ? "question" : "notify", importance: "high", source, notice_id: noticeId },
  };
}

/** A console with Home on: its tab registered, holding `entries`, and its transcript on screen. */
async function homeWith(entries: ChatMessage[]): Promise<string> {
  const homeId = `home-${++homeCount}`;
  queryClient.setQueryData(homeQueryKey(), { session_id: homeId, last_read_at: null });
  chatStore.ensureSession(homeId, "Home");
  chatStore.updateMessages(homeId, entries);
  Element.prototype.scrollIntoView = function (this: Element) { scrolled((this as HTMLElement).dataset.homeNotice); };
  scrolled.mockClear();
  await act(async () => root.render(
    <>
      <NoticeClickWatch go={go} />
      <div id={`pl-conv-${homeId}`}>
        <div className="pl-convo-scroll"><HomeTranscript homeId={homeId} /></div>
      </div>
    </>,
  ));
  await flush();
  return homeId;
}

/** Home's cards, read from the store at click time like the real transcript. */
function HomeTranscript({ homeId }: { homeId: string }) {
  const messages = chatStore.getSnapshot().sessions.find((session) => session.id === homeId)?.messages ?? [];
  return <>{messages.map((message) => message.home ? <HomeAlertCard key={message.id} message={message} home={message.home} /> : null)}</>;
}

const go = vi.fn();
const tap = (id: string, route = `/app/?notice=${id}`) =>
  listeners.forEach((fn) => fn({ data: { type: "protoagent:notice-click", v: 1, id, route } } as MessageEvent));
const settled = async () => { await flush(); await flush(); await act(async () => { await new Promise((r) => setTimeout(r, 150)); }); };

it("a tap whose id matches a Home entry selects Home and scrolls to that entry", async () => {
  const homeId = await homeWith([homeEntry("ntc_other"), homeEntry("ntc_7Qm2x")]);
  const before = chatStore.getSnapshot().currentSessionId;
  expect(before).not.toBe(homeId);
  useUI.getState().setSurface("work");
  useUI.getState().openGlobalSettings("devices");
  openDeepLink("ctx-1"); // the read-only Conversations pane would hide the chat
  tap("ntc_7Qm2x");
  await settled();
  expect(chatStore.getSnapshot().currentSessionId).toBe(homeId);
  expect(useUI.getState().surface).toBe("chat");
  expect(useUI.getState().mobileActive).toBe("chat");
  expect(useUI.getState().globalSettingsOpen).toBe(false);
  expect(snapshotForTest().stage).toBe("chat");
  expect(scrolled).toHaveBeenCalledWith("ntc_7Qm2x");
  expect(scrolled).not.toHaveBeenCalledWith("ntc_other");
  expect(fetchMock).not.toHaveBeenCalled(); // the transcript already showed it
  expect(go).not.toHaveBeenCalled();
  if (before) chatStore.switchSession(before);
});

it("a decision's tap lands on its Home entry, not straight on the answer form", async () => {
  const homeId = await homeWith([homeEntry("decision-dec-21", "decision")]);
  tap("decision-dec-21");
  await settled();
  expect(chatStore.getSnapshot().currentSessionId).toBe(homeId);
  expect(scrolled).toHaveBeenCalledWith("decision-dec-21");
  expect(openWorkGroup).not.toHaveBeenCalled();
});

it("reads Home's entries when the transcript doesn't show the tapped one yet", async () => {
  const homeId = await homeWith([]);
  homeRead = { session_id: homeId, entries: [{
    id: "home-entry-ntc_new", content: "[Home entry: notify]\nDisk is almost full",
    additional_kwargs: { protoagent_home_entry: { kind: "notify", importance: "normal", source: "watch", notice_id: "ntc_new" } },
  }] };
  expect(await openClickedNotice("ntc_new", "/app/?notice=ntc_new", go)).toBe(true);
  expect(chatStore.getSnapshot().currentSessionId).toBe(homeId);
  const merged = chatStore.getSnapshot().sessions.find((session) => session.id === homeId)!.messages;
  expect(merged.map((message) => message.home?.notice_id)).toEqual(["ntc_new"]);
  await act(async () => root.render(
    <div id={`pl-conv-${homeId}`}><div className="pl-convo-scroll"><HomeTranscript homeId={homeId} /></div></div>,
  ));
  await settled();
  expect(scrolled).toHaveBeenCalledWith("ntc_new"); // the card rendered after Home was selected
});

it("an unknown id navigates to the message's route", async () => {
  const homeId = await homeWith([homeEntry("ntc_other")]);
  homeRead = { session_id: homeId, entries: [] };
  go.mockClear();
  const before = chatStore.getSnapshot().currentSessionId;
  tap("ntc_unknown", "/app/agent/protoengineer-ba4c/?notice=ntc_unknown");
  await settled();
  expect(go).toHaveBeenCalledExactlyOnceWith("/app/agent/protoengineer-ba4c/?notice=ntc_unknown");
  expect(chatStore.getSnapshot().currentSessionId).toBe(before);
  expect(scrolled).not.toHaveBeenCalled();
});

it("an unknown id whose route is this page stays put instead of reloading the console", async () => {
  await homeWith([]);
  go.mockClear();
  tap("ntc_unknown"); // route: /app/?notice=ntc_unknown, and the console is on /app/
  await settled();
  expect(go).not.toHaveBeenCalled();
  expect(openWorkGroup).not.toHaveBeenCalled();
});

it.each([
  "https://elsewhere.example/app/", "/app/../etc/", "/app/agent/x/?notice=someone-else", "/app/agent/x/?next=/y", "/app/agent/x/#frag", 7,
])("never follows %j", async (route) => {
  go.mockClear();
  expect(await openClickedNotice("ntc_unknown", route, go)).toBe(false);
  expect(go).not.toHaveBeenCalled();
});

it("with Home off a decision's tap opens the answer form without asking for Home's entries", async () => {
  queryClient.setQueryData(homeQueryKey(), null); // ChatSurface's loaded "404: Home is off"
  expect(await openClickedNotice("decision-dec-30", "/app/?notice=decision-dec-30", go)).toBe(true);
  expect(fetchMock.mock.calls.map((call) => String(call[0])).some((url) => url.includes("/api/home"))).toBe(false);
  expect(openWorkGroup).toHaveBeenCalledWith("needs-you", "decision:dec-30");
});
