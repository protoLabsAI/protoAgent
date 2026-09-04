// Priority-now inbox fallback surfacing (#3351, third slice) — the console's Activity/Inbox UI
// must make a `now` page that is still PENDING (because its automatic self-A2A delivery failed)
// conspicuous and distinguishable, without letting a SUCCESSFULLY fired now item read as a
// pending page or double-count the unread state.
//
// The server contract this leans on (operator_api/console_handlers.py): a fired now item is
// marked delivered and pushes ONLY `activity.message`; a failed one is restored to pending and
// pushes `inbox.item {priority:"now"}`. So on the client, "pending now" ⇔ "blocked page".
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
import { isPendingNowInboxPage } from "../lib/queries";
import { ActivitySurface } from "./ActivitySurface";
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

const NOW_PAGE = inboxItem({
  id: 7,
  priority: "now",
  source: "webhook:deploy",
  text: "Prod deploy failed — page on-call",
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

describe("isPendingNowInboxPage (the blocked-page classifier)", () => {
  it("is true only for a pending now item — never a fired/accepted now, nor next/later", () => {
    // pending-now: still queued because delivery failed
    expect(isPendingNowInboxPage(inboxItem({ priority: "now", delivered_at: null }))).toBe(true);
    // fired-now: accepted turns are marked delivered before they ever return here
    expect(isPendingNowInboxPage(inboxItem({ priority: "now", delivered_at: "2026-09-03T10:01:00Z" }))).toBe(false);
    // ordinary queued items are not blocked pages
    expect(isPendingNowInboxPage(inboxItem({ priority: "next" }))).toBe(false);
    expect(isPendingNowInboxPage(inboxItem({ priority: "later" }))).toBe(false);
  });
});

describe("ActivityWidget — pill signalling", () => {
  it("flags a blocked priority-now page on the pill even while the feed is closed", () => {
    act(() => root.render(h(ActivityWidget)));
    expect(testid("activity-badge")).toBeNull();

    emit("inbox.item", { id: 7, priority: "now", source: "webhook:deploy", text: "page on-call" });

    const badge = testid("activity-badge")!;
    expect(badge).not.toBeNull();
    expect(badge.textContent).toBe("1");
    expect(badge.classList.contains("activity-badge--alert")).toBe(true);
    expect(badge.getAttribute("data-alert")).toBe("now");
    expect(testid("activity-widget")!.getAttribute("aria-label")).toContain("needs delivery");
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
  it("renders a pending now item as a conspicuous blocked page with full context", async () => {
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
