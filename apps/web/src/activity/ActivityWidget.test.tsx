// Priority-now inbox fallback surfacing (#3351, third slice; retry-when-free update) — a `now`
// page that is still PENDING is QUEUED: the server retries it on its own once the agent is free,
// so it renders as a calm queued row, and only an OPERATOR-CLASS page (`needs_human`) keeps the
// conspicuous blocked-page/alert treatment. A SUCCESSFULLY fired now item must never read as a
// pending page or double-count the unread state.
//
// The server contract this leans on (operator_api/console_handlers.py): a fired now item is
// marked delivered and pushes ONLY `activity.message`; a failed one is restored to pending and
// pushes `inbox.item {priority:"now", needs_human}`. So on the client, "pending now" ⇔ "queued
// page", and "pending now + needs_human" ⇔ "alert".
//
// createRoot/act + a mocked event bus, in the style of ServerTurnWatch.test.tsx / FleetRoom.test.tsx
// (no testing-library dep). The event mock supports several subscribers per topic since both the
// widget and the surface subscribe to `inbox.item` and `activity.message`.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ActivityHistory, InboxItem } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listeners: new Map<string, Array<(data: Record<string, unknown>) => void>>(),
}));

vi.mock("../lib/events", () => ({
  onServerEvent: (topic: string, fn: (data: Record<string, unknown>) => void) => {
    const arr = mocks.listeners.get(topic) ?? [];
    arr.push(fn);
    mocks.listeners.set(topic, arr);
    return () => {
      mocks.listeners.set(topic, (mocks.listeners.get(topic) ?? []).filter((f) => f !== fn));
    };
  },
}));

// The completed-timeline markdown renderer + the full-screen reader are irrelevant here and pull
// heavy chunks — stub them so the suite stays fast and deterministic.
vi.mock("../chat/LazyMarkdown", () => ({
  Markdown: ({ children }: { children: string }) => h("div", { className: "md" }, children),
}));
vi.mock("../docviewer", () => ({ openDocument: vi.fn() }));

import { api } from "../lib/api";
import { isPendingNowInboxPage, needsOperatorAttention } from "../lib/queries";
import { openDocument } from "../docviewer";
import { ActivitySurface, stripWakeHeader } from "./ActivitySurface";
import { ActivityWidget } from "./ActivityWidget";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function inboxItem(over: Partial<InboxItem> = {}): InboxItem {
  return {
    id: 1,
    created_at: "2026-09-03T10:00:00Z",
    priority: "next",
    source: "a2a:sister",
    text: "an ordinary queued stimulus",
    dedup_key: null,
    delivered_at: null,
    ...over,
  };
}

// Operator-class: only a needs_human page keeps the blocked-page alert treatment.
const NOW_PAGE = inboxItem({
  id: 7,
  priority: "now",
  source: "webhook:deploy",
  text: "Prod deploy failed — page on-call",
  needs_human: true,
});
// An ordinary undelivered page: queued, retries on its own when the agent is free.
const QUEUED_PAGE = inboxItem({
  id: 9,
  priority: "now",
  source: "a2a:sister",
  text: "Board update — new card assigned",
  attempts: 2,
});

const NO_ACTIVITY: ActivityHistory = { context_id: "s1", entries: [], messages: [] };

let container: HTMLElement;
let root: Root;

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function emit(topic: string, data: Record<string, unknown>) {
  act(() => {
    (mocks.listeners.get(topic) ?? []).forEach((fn) => fn(data));
  });
}

const testid = (id: string) => container.querySelector(`[data-testid="${id}"]`);

beforeEach(() => {
  // ActivitySurface auto-scrolls its feed; jsdom doesn't implement element scrolling.
  if (!HTMLElement.prototype.scrollTo) {
    (HTMLElement.prototype as unknown as { scrollTo: () => void }).scrollTo = () => {};
  }
  mocks.listeners.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("isPendingNowInboxPage / needsOperatorAttention (the queued/alert classifiers)", () => {
  it("is true only for a pending now item — never a fired/accepted now, nor next/later", () => {
    // pending-now: still queued because delivery failed
    expect(isPendingNowInboxPage(inboxItem({ priority: "now", delivered_at: null }))).toBe(true);
    // fired-now: accepted turns are marked delivered before they ever return here
    expect(isPendingNowInboxPage(inboxItem({ priority: "now", delivered_at: "2026-09-03T10:01:00Z" }))).toBe(false);
    // ordinary queued items are not blocked pages
    expect(isPendingNowInboxPage(inboxItem({ priority: "next" }))).toBe(false);
    expect(isPendingNowInboxPage(inboxItem({ priority: "later" }))).toBe(false);
  });
  it("alerts only for an operator-class (needs_human) pending now page", () => {
    expect(needsOperatorAttention(NOW_PAGE)).toBe(true);
    expect(needsOperatorAttention(QUEUED_PAGE)).toBe(false); // queued — retries when the agent is free
    expect(needsOperatorAttention({ ...NOW_PAGE, delivered_at: "2026-09-03T10:01:00Z" })).toBe(false);
    expect(needsOperatorAttention(inboxItem({ priority: "next", needs_human: true }))).toBe(false);
  });
});

describe("ActivityWidget — pill signalling", () => {
  it("flags an operator-class (needs_human) priority-now page even while the feed is closed", () => {
    act(() => root.render(h(ActivityWidget)));
    expect(testid("activity-badge")).toBeNull();

    emit("inbox.item", { id: 7, priority: "now", source: "webhook:deploy", text: "page on-call", needs_human: true });

    const badge = testid("activity-badge")!;
    expect(badge).not.toBeNull();
    // The badge is now the DS Count primitive (span.pl-count), keeping the alert modifier on top.
    expect(badge.classList.contains("pl-count")).toBe(true);
    expect(badge.textContent).toBe("1");
    expect(badge.classList.contains("activity-badge--alert")).toBe(true);
    expect(badge.getAttribute("data-alert")).toBe("now");
    expect(testid("activity-widget")!.getAttribute("aria-label")).toContain("needs delivery");
  });

  it("treats a merely undelivered now page as plain unread — queued, never an alert", () => {
    act(() => root.render(h(ActivityWidget)));

    // The restored-page push without needs_human: it retries when the agent is free.
    emit("inbox.item", { id: 9, priority: "now", source: "a2a:sister", text: "card assigned" });

    const badge = testid("activity-badge")!;
    expect(badge.textContent).toBe("1");
    expect(badge.classList.contains("activity-badge--alert")).toBe(false);
    expect(badge.getAttribute("data-alert")).toBeNull();
    expect(testid("activity-widget")!.getAttribute("aria-label")).toBe("Feed — 1 new");
  });

  it("counts a fired now item once, as activity, without the blocked-page alert", () => {
    act(() => root.render(h(ActivityWidget)));

    // A fired now pushes ONLY activity.message (never inbox.item) — one unread bump, no alert.
    emit("activity.message", { priority: "now", text: "handled the now item" });

    const badge = testid("activity-badge")!;
    expect(badge.textContent).toBe("1");
    expect(badge.classList.contains("activity-badge--alert")).toBe(false);
    expect(badge.getAttribute("data-alert")).toBeNull();
    expect(testid("activity-widget")!.getAttribute("aria-label")).toBe("Feed — 1 new");
  });

  it("does not flag a delivered now inbox payload as a blocked page", () => {
    act(() => root.render(h(ActivityWidget)));

    emit("inbox.item", {
      id: 8,
      priority: "now",
      delivered_at: "2026-09-03T10:01:00Z",
      source: "a2a",
      text: "already delivered",
    });

    const badge = testid("activity-badge")!;
    expect(badge.textContent).toBe("1");
    expect(badge.classList.contains("activity-badge--alert")).toBe(false);
    expect(badge.getAttribute("data-alert")).toBeNull();
  });

  it("treats ordinary next/later inbox items as plain unread with no alert", () => {
    act(() => root.render(h(ActivityWidget)));

    emit("inbox.item", { id: 1, priority: "next", source: "a2a", text: "fyi" });
    let badge = testid("activity-badge")!;
    expect(badge.textContent).toBe("1");
    expect(badge.classList.contains("activity-badge--alert")).toBe(false);

    emit("inbox.item", { id: 2, priority: "later", source: "a2a", text: "later" });
    badge = testid("activity-badge")!;
    expect(badge.textContent).toBe("2");
    expect(badge.classList.contains("activity-badge--alert")).toBe(false);
    expect(testid("activity-widget")!.getAttribute("aria-label")).toBe("Feed — 2 new");
  });
});

describe("ActivitySurface — pending feed", () => {
  it("renders an operator-class pending now item as a conspicuous blocked page with full context", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [NOW_PAGE, inboxItem({ id: 2, priority: "next" })] });
    vi.spyOn(api, "activity").mockResolvedValue(NO_ACTIVITY);

    act(() => root.render(h(ActivitySurface)));
    await flush();

    const stuck = container.querySelectorAll('[data-testid="inbox-now-page"]');
    expect(stuck.length).toBe(1);
    const stuckText = stuck[0].textContent || "";
    expect(stuckText).toContain("undelivered page");
    expect(stuckText).toContain("now");
    expect(stuckText).toContain("webhook:deploy");
    expect(stuckText).toContain("Prod deploy failed");

    // The ordinary next item renders as a plain, un-flagged inbox item.
    const plain = container.querySelectorAll(".inbox-item:not(.inbox-item--stuck)");
    expect(plain.length).toBe(1);
    expect(plain[0].textContent).toContain("next");
    expect(plain[0].querySelector(".inbox-stuck-flag")).toBeNull();
  });

  it("renders a merely undelivered now item as a calm queued row, never an alert", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [QUEUED_PAGE] });
    vi.spyOn(api, "activity").mockResolvedValue(NO_ACTIVITY);

    act(() => root.render(h(ActivitySurface)));
    await flush();

    // No blocked-page alert — the server retries this on its own when the agent is free.
    expect(container.querySelector('[data-testid="inbox-now-page"]')).toBeNull();
    const row = container.querySelector('[data-testid="inbox-queued-now"]')!;
    expect(row).not.toBeNull();
    expect(row.classList.contains("inbox-item--stuck")).toBe(false);
    expect(row.textContent).toContain("Queued — runs when the agent is free");
    expect(row.textContent).toContain("Board update");
    // queued_since + attempts ride the hover detail.
    expect(row.querySelector(".inbox-queued-flag")!.getAttribute("title")).toContain("2 delivery attempts");
  });

  it("keeps ordinary next/later queued items rendering unchanged (no blocked page)", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({
      items: [inboxItem({ id: 1, priority: "next" }), inboxItem({ id: 2, priority: "later" })],
    });
    vi.spyOn(api, "activity").mockResolvedValue(NO_ACTIVITY);

    act(() => root.render(h(ActivitySurface)));
    await flush();

    expect(container.querySelector('[data-testid="inbox-now-page"]')).toBeNull();
    expect(container.querySelectorAll(".inbox-item").length).toBe(2);
    expect(container.querySelectorAll(".inbox-item--stuck").length).toBe(0);
  });

  it("shows a fired now as a completed activity entry, never a pending page", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [] });
    vi.spyOn(api, "activity").mockResolvedValue(NO_ACTIVITY);

    act(() => root.render(h(ActivitySurface)));
    await flush();

    emit("activity.message", { priority: "now", origin: "inbox", text: "did the now thing" });

    expect(container.querySelector('[data-testid="inbox-now-page"]')).toBeNull();
    expect(testid("feed-completed")!.textContent).toContain("did the now thing");
  });

  it("dismissing a blocked page is an explicit deliver, not a silent one", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [NOW_PAGE] });
    vi.spyOn(api, "activity").mockResolvedValue(NO_ACTIVITY);
    const deliver = vi.spyOn(api, "deliverInbox").mockResolvedValue({ ok: true, delivered: 1 });

    act(() => root.render(h(ActivitySurface)));
    await flush();

    // The dismiss control is the only button inside a blocked-page card.
    const btn = container.querySelector<HTMLButtonElement>('[data-testid="inbox-now-page"] button')!;
    expect(btn).not.toBeNull();
    act(() => btn.click());

    expect(deliver).toHaveBeenCalledWith(7);
    expect(container.querySelector('[data-testid="inbox-now-page"]')).toBeNull();
  });
});

// #5094: the scheduler prepends its internal "[Autonomous wake — …]" header to a
// scheduled/watch/wait/goal prompt. The Work Activity row title used to strip it; the
// feed dialog is the timeline's only home since S4c, so the stripping lives here now —
// in the stimulus row, its hover text and the full-screen reader content.
describe("ActivitySurface — wake-header stripping (#5094)", () => {
  const WAKE = "[Autonomous wake — scheduled run. Orient from <working_state>, then:]";
  const entry = (over: Partial<ActivityHistory["entries"][number]>) => ({
    id: 1, created_at: "2026-09-03T10:00:00Z", origin: "scheduler", trigger: "heartbeat",
    priority: "", state: "completed", task_id: "", text: "done", stimulus: "", ...over,
  });

  it("drops every scheduler wake-header variant, keeps an ordinary stimulus verbatim", () => {
    for (const header of [
      WAKE,
      "[Autonomous wake — a watch you set has tripped. Orient from <working_state>, then:]",
      "[Autonomous wake — a wait you scheduled has elapsed. Continue:]",
      "[Autonomous wake — driving your active goal. Orient from <working_state>, then:]",
    ]) {
      expect(stripWakeHeader(`${header}\n\nCheck the board for blocked cards.`))
        .toBe("Check the board for blocked cards.");
    }
    expect(stripWakeHeader("CI webhook: build #4821 failed")).toBe("CI webhook: build #4821 failed");
    expect(stripWakeHeader(WAKE)).toBe("");
    expect(stripWakeHeader(undefined)).toBe("");
  });

  it("renders a persisted scheduled turn's stimulus (row + hover) without the header", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [] });
    vi.spyOn(api, "activity").mockResolvedValue({
      context_id: "s1", messages: [],
      entries: [entry({ stimulus: `${WAKE}\n\nCheck the board for blocked cards.` })],
    });

    act(() => root.render(h(ActivitySurface)));
    await flush();

    const row = container.querySelector(".activity-stimulus")!;
    expect(row.querySelector(".activity-stimulus-text")!.textContent).toBe("Check the board for blocked cards.");
    expect(row.getAttribute("title")).toBe("Check the board for blocked cards.");
    expect(container.textContent).not.toContain("Autonomous wake");
  });

  it("strips a live bus turn's header too, including the reader content", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [] });
    vi.spyOn(api, "activity").mockResolvedValue(NO_ACTIVITY);

    act(() => root.render(h(ActivitySurface)));
    await flush();
    emit("activity.message", {
      text: "done", origin: "scheduler", trigger: "heartbeat",
      stimulus: `${WAKE}\n\nHourly heartbeat check.`,
    });

    expect(container.querySelector(".activity-stimulus-text")!.textContent).toBe("Hourly heartbeat check.");
    act(() => container.querySelector<HTMLButtonElement>(".activity-entry-open")!.click());
    const doc = vi.mocked(openDocument).mock.lastCall![0];
    expect(doc.content).toContain("Hourly heartbeat check.");
    expect(doc.content).not.toContain("Autonomous wake");
  });

  it("omits the stimulus row when the stimulus was nothing but the header", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [] });
    vi.spyOn(api, "activity").mockResolvedValue({
      context_id: "s1", messages: [], entries: [entry({ stimulus: WAKE })],
    });

    act(() => root.render(h(ActivitySurface)));
    await flush();

    expect(container.querySelector(".activity-stimulus")).toBeNull();
    // The response itself still renders — only the framing is dropped.
    expect(testid("feed-completed")!.textContent).toContain("done");
  });
});
