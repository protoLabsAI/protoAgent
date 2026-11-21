import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chatStore } from "../chat/chat-store";
import { homeQueryKey } from "../chat/homeTab";
import { hydrateDurableChatSessions } from "../chat/sessionHydration";
import { api } from "../lib/api";
import { queryClient } from "../lib/queryClient";

const mocks = vi.hoisted(() => ({
  handlers: new Map<string, (data: Record<string, unknown>) => void>(),
  toast: vi.fn(),
  notifyIfHidden: vi.fn(),
  replay: false,
}));

vi.mock("../lib/events", async (original) => ({
  ...(await original<typeof import("../lib/events")>()),
  eventIsReplay: () => mocks.replay,
  onTopic: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    mocks.handlers.set(topic, fn);
    return () => mocks.handlers.delete(topic);
  },
}));
vi.mock("@protolabsai/ui/overlays", () => ({ useToast: () => mocks.toast }));
vi.mock("../lib/notify", () => ({ notifyIfHidden: mocks.notifyIfHidden }));

import { homeEntryId, ScheduledWatch } from "./ScheduledWatch";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const FIRED_AT = "2026-11-19T12:00:00Z";

/** A `scheduler.completed` event as `server/a2a.py` publishes it. */
function fire(session: string, overrides: Record<string, unknown> = {}) {
  return {
    job_id: "job-1", origin_session: session, fired_at: FIRED_AT, summary: "Disk is 71% full",
    status: "completed", collapse: false, activity_context: "job:job-1", task_id: "task-1",
    notice_id: "notice-1", ...overrides,
  };
}

/** The Home entry the server writes for that fire's notice (`graph/home_entries.py`). */
function storedEntry(noticeId = "notice-1") {
  return {
    id: `home-entry-${noticeId}`,
    content: "[Home entry: review from scheduler, importance normal. For reference; not a request.]\nScheduled run job-1 completed: Disk is 71% full",
    additional_kwargs: {
      protoagent_home_entry: { kind: "review", importance: "normal", source: "scheduler", notice_id: noticeId },
      protoagent_sent_at: FIRED_AT,
    },
  };
}

const messagesOf = (id: string) => chatStore.getSnapshot().sessions.find((s) => s.id === id)!.messages;
const emit = (data: Record<string, unknown>) => act(() => mocks.handlers.get("scheduler.completed")!(data));

/** A full hydration pass whose Home read answers with `entries`; waits for the merge. */
async function hydrate(homeId: string, sessionIds: string[], entries: ReturnType<typeof storedEntry>[]) {
  const homeRead = vi.spyOn(api, "homeEntries").mockResolvedValue({ session_id: homeId, entries });
  vi.spyOn(api, "chatSessions").mockResolvedValue({
    sessions: sessionIds.map((session_id) => ({ session_id, last_updated: FIRED_AT, turn_count: 0 })),
  });
  vi.spyOn(api, "chatSessionTurns").mockResolvedValue({ turns: [] });
  await hydrateDurableChatSessions();
  await homeRead.mock.results[homeRead.mock.results.length - 1].value;
  await Promise.resolve();
}

describe("ScheduledWatch", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    mocks.replay = false;
    sessionStorage.clear();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(h(ScheduledWatch)));
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    queryClient.clear();
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  function openHome(id: string) {
    chatStore.ensureSession(id, "Home");
    queryClient.setQueryData(homeQueryKey(), { session_id: id });
  }

  it("shows a fire into Home once: hydration replaces the live card with the stored entry", async () => {
    const home = "chat-home-live-fire";
    openHome(home);

    emit(fire(home));
    // Live: the card is on screen at once, keyed by the entry the server is writing.
    expect(messagesOf(home)).toEqual([
      expect.objectContaining({ id: homeEntryId("notice-1"), scheduled: expect.objectContaining({ jobId: "job-1" }) }),
    ]);
    expect(mocks.toast).toHaveBeenCalledTimes(1);

    await hydrate(home, [home], [storedEntry()]);
    expect(messagesOf(home)).toHaveLength(1);
    expect(messagesOf(home)[0]).toMatchObject({
      id: "home-entry-notice-1", home: { source: "scheduler", notice_id: "notice-1" },
    });

    // A later hydration (a reload re-reads the same entry) still leaves one card.
    await hydrate(home, [home], [storedEntry()]);
    expect(messagesOf(home)).toHaveLength(1);
  });

  it("keeps the live Home card when the server has not written the entry yet", async () => {
    const home = "chat-home-entry-pending";
    openHome(home);
    emit(fire(home));
    await hydrate(home, [home], []);
    expect(messagesOf(home).map((message) => message.id)).toEqual(["home-entry-notice-1"]);
    await hydrate(home, [home], [storedEntry()]);
    expect(messagesOf(home)).toHaveLength(1);
  });

  it("does not add a card when Home already holds the fire's stored entry", async () => {
    const home = "chat-home-entry-first";
    openHome(home);
    await hydrate(home, [home], [storedEntry()]);
    expect(messagesOf(home)).toHaveLength(1);

    emit(fire(home));
    expect(messagesOf(home)).toHaveLength(1);
    expect(messagesOf(home)[0].home).toBeDefined();
    // The result is in front of the operator, so they are still told it ran.
    expect(mocks.toast).toHaveBeenCalledTimes(1);
  });

  it("gives each Home fire of a recurring job its own card", () => {
    const home = "chat-home-recurring";
    openHome(home);
    emit(fire(home));
    emit(fire(home, { fired_at: "2026-11-19T13:00:00Z", notice_id: "notice-2", collapse: true }));
    expect(messagesOf(home).map((message) => message.id)).toEqual(["home-entry-notice-1", "home-entry-notice-2"]);
  });

  it("falls back to the display-only id for a Home fire that carries no notice id", () => {
    const home = "chat-home-no-notice";
    openHome(home);
    emit(fire(home, { notice_id: undefined }));
    expect(messagesOf(home).map((message) => message.id)).toEqual([`sched-job-1-${FIRED_AT}`]);
  });

  it("leaves a fire into a non-Home origin chat exactly as before", async () => {
    const home = "chat-home-bystander";
    const origin = "chat-origin-plain";
    openHome(home);
    chatStore.ensureSession(origin, "Disk checks");
    const data = fire(origin);

    emit(data);
    const card = {
      id: `sched-job-1-${FIRED_AT}`,
      role: "system",
      content: "Scheduled task job-1 ran",
      status: "done",
      scheduled: {
        jobId: "job-1", firedAt: FIRED_AT, summary: "Disk is 71% full", status: "completed",
        collapse: false, activityContext: "job:job-1", taskId: "task-1",
      },
    };
    expect(messagesOf(origin)).toEqual([{ ...card, createdAt: expect.any(Number) }]);
    expect(messagesOf(home)).toEqual([]);
    expect(mocks.toast).toHaveBeenCalledWith({ tone: "success", title: "Scheduled task ran", message: "Job job-1" });
    expect(mocks.notifyIfHidden).toHaveBeenCalledWith(
      "Scheduled task ran", "Job job-1", undefined, { notice: true, noticeId: "notice-1" },
    );

    // The replay ring re-delivering the same fire adds nothing.
    emit(data);
    expect(messagesOf(origin)).toHaveLength(1);

    // Hydration puts the server's Home entry in Home and leaves the origin chat's card alone.
    await hydrate(home, [home, origin], [storedEntry()]);
    expect(messagesOf(origin)).toEqual([{ ...card, createdAt: expect.any(Number) }]);
    expect(messagesOf(home).map((message) => message.id)).toEqual(["home-entry-notice-1"]);
  });

  it("a fire replayed to a console that was closed lands as its card, with no toast or notification", () => {
    const chat = "chat-replayed-fire";
    chatStore.ensureSession(chat, "Disk check");
    mocks.replay = true; // an event from before this console opened (lib/events `eventIsReplay`)
    emit(fire(chat, { notice_id: "notice-replayed" }));
    expect(messagesOf(chat)).toEqual([expect.objectContaining({ scheduled: expect.objectContaining({ jobId: "job-1" }) })]);
    expect(mocks.toast).not.toHaveBeenCalled();
    expect(mocks.notifyIfHidden).not.toHaveBeenCalled();
  });

  it("ignores a fire whose chat is not open in this window", () => {
    openHome("chat-home-elsewhere");
    emit(fire("chat-not-open"));
    expect(chatStore.getSnapshot().sessions.some((s) => s.id === "chat-not-open")).toBe(false);
    expect(mocks.toast).not.toHaveBeenCalled();
  });
});
