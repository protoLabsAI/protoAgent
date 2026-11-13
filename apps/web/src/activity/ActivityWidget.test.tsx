// The merged Feed (docs/design/feed.md, slice 4): ONE newest-first list where each thing
// the agent was asked to do appears exactly once — a pending inbox item is a quiet queued
// row, a live `activity.message` with its `inbox_id` turns that row into its result, a
// failed turn carries one "Failed" badge. Nothing in the Feed dismisses, delivers or
// alerts (bd-rbk7e retries undelivered pages server-side).
//
// The PILL is a plain unread count (slice 6): a priority-now page bumps it exactly like
// any other item — the alert badge/label are gone (anything operator-class pages through
// Work › Needs you). The classifier tests stay here since the Needs-you Delivery lane
// (work/tone.ts) still consumes `needsOperatorAttention`.
//
// createRoot/act + a mocked event bus, in the style of ServerTurnWatch.test.tsx /
// FleetRoom.test.tsx (no testing-library dep). The event mock supports several
// subscribers per topic since both the widget and the surface subscribe to `inbox.item`
// and `activity.message`.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ActivityEntry, ActivityHistory, InboxItem, RuntimeStatus } from "../lib/types";

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

import { api } from "../lib/api";
import { isPendingNowInboxPage, needsOperatorAttention } from "../lib/queries";
import { ActivitySurface, stripWakeHeader } from "./ActivitySurface";
import { ActivityWidget } from "./ActivityWidget";
import type { FeedRow } from "./feedModel";

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

function entry(over: Partial<ActivityEntry> = {}): ActivityEntry {
  return {
    id: 10, created_at: "2026-09-03T11:00:00Z", origin: "scheduler", trigger: "heartbeat",
    priority: "", state: "completed", text: "done", task_id: "", stimulus: "", ...over,
  };
}

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

// The surface reads the agent's display name off the shared runtime probe (the
// queued-row promise + the live announcement), so surface renders need a provider.
function renderSurface(props: { onOpenDetail?: (row: FeedRow) => void } = {}) {
  vi.spyOn(api, "runtimeStatus").mockResolvedValue({
    identity: { name: "roxy", operator: "" },
  } as unknown as RuntimeStatus);
  act(() =>
    root.render(
      h(
        QueryClientProvider,
        { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) },
        h(ActivitySurface, props),
      ),
    ),
  );
}

beforeEach(() => {
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
    const page = inboxItem({ id: 7, priority: "now", needs_human: true });
    expect(needsOperatorAttention(page)).toBe(true);
    expect(needsOperatorAttention(inboxItem({ id: 9, priority: "now" }))).toBe(false); // queued — retries when free
    expect(needsOperatorAttention({ ...page, delivered_at: "2026-09-03T10:01:00Z" })).toBe(false);
    expect(needsOperatorAttention(inboxItem({ priority: "next", needs_human: true }))).toBe(false);
  });
});

describe("ActivityWidget — pill unread count (slice 6: plain count, no alert path)", () => {
  // The pill reads the agent's display name off the shared runtime probe for its resting
  // hover info, so widget renders need a provider too.
  function renderWidget() {
    vi.spyOn(api, "runtimeStatus").mockResolvedValue({
      identity: { name: "roxy", operator: "" },
    } as unknown as RuntimeStatus);
    act(() =>
      root.render(
        h(
          QueryClientProvider,
          { client: new QueryClient({ defaultOptions: { queries: { retry: false } } }) },
          h(ActivityWidget),
        ),
      ),
    );
  }

  it("bumps a plain count for a priority-now page exactly like any other item — never an alert", async () => {
    renderWidget();
    await flush();
    expect(testid("activity-badge")).toBeNull();

    // Operator-class (needs_human) priority-now page: the pill alert is gone — anything
    // operator-class pages through Work › Needs you, never the Feed pill (feed.md §2).
    emit("inbox.item", { id: 7, priority: "now", source: "webhook:deploy", text: "page on-call", needs_human: true });

    const badge = testid("activity-badge")!;
    expect(badge).not.toBeNull();
    // The badge is the bare DS Count primitive (span.pl-count) — no alert modifier left.
    expect(badge.classList.contains("pl-count")).toBe(true);
    expect(badge.textContent).toBe("1");
    expect(container.querySelector(".activity-badge--alert")).toBeNull();
    expect(container.querySelector("[data-alert]")).toBeNull();
    expect(testid("activity-widget")!.getAttribute("aria-label")).toBe("Feed — 1 new");

    // An ordinary next item bumps the SAME count the same way.
    emit("inbox.item", { id: 1, priority: "next", source: "a2a", text: "fyi" });
    expect(testid("activity-badge")!.textContent).toBe("2");
    expect(testid("activity-widget")!.getAttribute("aria-label")).toBe("Feed — 2 new");
  });

  it("counts a fired now item once, as activity, and caps the badge at 9+", async () => {
    renderWidget();
    await flush();

    // A fired now pushes ONLY activity.message (never inbox.item) — one unread bump.
    emit("activity.message", { priority: "now", text: "handled the now item" });
    expect(testid("activity-badge")!.textContent).toBe("1");
    expect(testid("activity-widget")!.getAttribute("aria-label")).toBe("Feed — 1 new");

    for (let i = 0; i < 9; i++) emit("activity.message", { text: `turn ${i}` });
    expect(testid("activity-badge")!.textContent).toBe("9+");
  });
});

describe("ActivitySurface — one merged feed (docs/design/feed.md §1, §4)", () => {
  it("shows a pending item as one quiet queued row — no badge, pill or button inside it", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({
      items: [inboxItem({ id: 5, priority: "now", text: "Deploy the docs" })],
    });
    vi.spyOn(api, "activity").mockResolvedValue(NO_ACTIVITY);

    renderSurface();
    await flush();

    const rows = container.querySelectorAll(".feed-row");
    expect(rows.length).toBe(1);
    expect(rows[0].getAttribute("data-kind")).toBe("queued");
    expect(rows[0].textContent).toContain("Deploy the docs");
    // brandName() display-cases the configured lower-case slug: roxy → Roxy.
    expect(rows[0].textContent).toContain("Queued — runs when Roxy is free");
    expect(rows[0].querySelector(".pl-badge")).toBeNull();
    expect(rows[0].querySelector(".inbox-pri")).toBeNull();
    // The row IS the control — nothing interactive nests inside it.
    expect(rows[0].querySelector("button")).toBeNull();
    expect(rows[0].getAttribute("aria-label")).toContain("Open details");
  });

  it("turns the queued row into its result on activity.message with its inbox_id — one row, announced, stable across refetch", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({
      items: [inboxItem({ id: 5, created_at: new Date().toISOString(), text: "Deploy the docs" })],
    });
    vi.spyOn(api, "activity").mockResolvedValue(NO_ACTIVITY);

    renderSurface();
    await flush();
    expect(container.querySelectorAll(".feed-row").length).toBe(1);

    emit("activity.message", {
      text: "Docs deployed.\nAll green.", origin: "inbox", trigger: "github", inbox_id: 5,
    });

    let rows = container.querySelectorAll(".feed-row");
    expect(rows.length).toBe(1);
    expect(rows[0].getAttribute("data-kind")).toBe("ran");
    expect(rows[0].textContent).toContain("Docs deployed.");
    expect(rows[0].textContent).not.toContain("Queued —");
    // §8: the one polite live region announces a queued row turning into its result.
    expect(container.querySelector(".feed-announce")!.textContent).toBe("Roxy finished: Docs deployed.");

    // A later inbox push refetches (the mock still returns the pending item) — the live
    // entry keeps claiming it: still exactly one row.
    emit("inbox.item", { id: 6 });
    await flush();
    rows = container.querySelectorAll(".feed-row");
    expect(rows.length).toBe(1);
    expect(rows[0].getAttribute("data-kind")).toBe("ran");
  });

  it("shows the entry, not the item, when a persisted entry claims it via inbox_id (fresh hydration)", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [inboxItem({ id: 5, text: "Deploy the docs" })] });
    vi.spyOn(api, "activity").mockResolvedValue({
      ...NO_ACTIVITY,
      entries: [entry({ origin: "inbox", trigger: "github", text: "Docs deployed.", inbox_id: 5 })],
    });

    renderSurface();
    await flush();

    const rows = container.querySelectorAll(".feed-row");
    expect(rows.length).toBe(1);
    expect(rows[0].getAttribute("data-kind")).toBe("ran");
    expect(rows[0].textContent).toContain("Docs deployed.");
    // No replacement happened live — nothing to announce.
    expect(container.querySelector(".feed-announce")!.textContent).toBe("");
  });

  it("marks a failed entry with exactly one Failed badge and an error dot", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [] });
    vi.spyOn(api, "activity").mockResolvedValue({
      ...NO_ACTIVITY,
      entries: [entry({ state: "failed", text: "Second target timed out.\nDetails follow." })],
    });

    renderSurface();
    await flush();

    const row = container.querySelector('.feed-row[data-kind="failed"]')!;
    expect(row).not.toBeNull();
    const badges = row.querySelectorAll(".pl-badge--error");
    expect(badges.length).toBe(1);
    expect(badges[0].textContent).toBe("Failed");
    expect(row.querySelector(".pl-dot--error")).not.toBeNull();
    expect(row.textContent).toContain("Second target timed out.");
    expect(row.getAttribute("aria-label")).toContain("Failed: Second target timed out.");
  });

  it("dismisses nothing and never delivers: clicking rows only opens the detail", async () => {
    const deliver = vi.spyOn(api, "deliverInbox").mockResolvedValue({ ok: true, delivered: 1 });
    vi.spyOn(api, "inbox").mockResolvedValue({
      items: [inboxItem({ id: 7, priority: "now", needs_human: true, text: "Prod deploy failed" })],
    });
    vi.spyOn(api, "activity").mockResolvedValue({ ...NO_ACTIVITY, entries: [entry()] });
    const onOpenDetail = vi.fn();

    renderSurface({ onOpenDetail });
    await flush();

    const buttons = container.querySelectorAll<HTMLButtonElement>('[data-testid="unified-feed"] button');
    expect(buttons.length).toBe(2); // the two rows — no dismiss control anywhere
    act(() => buttons.forEach((b) => b.click()));
    expect(deliver).not.toHaveBeenCalled();
    expect(onOpenDetail).toHaveBeenCalledTimes(2);
    expect(container.querySelectorAll(".feed-row").length).toBe(2); // nothing hidden by a click
  });

  it("renders newest first under h3 day headings and never auto-scrolls", async () => {
    const scrolled = vi.fn();
    (HTMLElement.prototype as unknown as { scrollTo: typeof scrolled }).scrollTo = scrolled;
    // Local-time stamps so day grouping (local) and parsing (UTC) agree in any zone.
    const at = (d: number, hour: number) => new Date(2026, 8, d, hour).toISOString();
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [inboxItem({ id: 5, created_at: at(2, 9), text: "Queued thing" })] });
    vi.spyOn(api, "activity").mockResolvedValue({
      ...NO_ACTIVITY,
      entries: [entry({ id: 1, created_at: at(1, 8), text: "oldest" }), entry({ id: 2, created_at: at(3, 10), text: "newest" })],
    });

    renderSurface();
    await flush();

    const headings = [...container.querySelectorAll(".feed-day-heading")];
    expect(headings.map((n) => n.tagName)).toEqual(["H3", "H3", "H3"]);
    expect(headings.map((n) => n.textContent)).toEqual(["Sep 3", "Sep 2", "Sep 1"]);
    expect([...container.querySelectorAll(".feed-row")].map((r) => r.getAttribute("data-kind")))
      .toEqual(["ran", "queued", "ran"]);
    expect(container.querySelectorAll(".feed-day-rows")[0].tagName).toBe("OL");
    expect(scrolled).not.toHaveBeenCalled();
  });

  it("renders the empty, loading and couldn't-load states (§7)", async () => {
    // Loading: skeleton rows + aria-busy while the fetches are in flight.
    vi.spyOn(api, "inbox").mockImplementation(() => new Promise(() => {}));
    vi.spyOn(api, "activity").mockImplementation(() => new Promise(() => {}));
    renderSurface();
    expect(testid("feed-list")!.getAttribute("aria-busy")).toBe("true");
    expect(testid("feed-skeleton")).not.toBeNull();

    // Empty: both loaded, nothing to show.
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [] });
    vi.spyOn(api, "activity").mockResolvedValue(NO_ACTIVITY);
    act(() => root.unmount());
    root = createRoot(container);
    renderSurface();
    await flush();
    expect(container.querySelector(".pl-empty")!.textContent).toContain("Nothing here yet");
    expect(container.querySelector(".pl-empty")!.textContent).toContain("newest first");
    expect(container.querySelector(".pl-alert--error")).toBeNull();

    // Partial failure: one alert above whatever loaded — the queued row stays visible.
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [inboxItem({ id: 5, text: "Still here" })] });
    vi.spyOn(api, "activity").mockRejectedValue(new Error("boom"));
    act(() => root.unmount());
    root = createRoot(container);
    renderSurface();
    await flush();
    const alerts = container.querySelectorAll(".pl-alert--error");
    expect(alerts.length).toBe(1);
    expect(alerts[0].textContent).toContain("Couldn't load the feed. Check the connection, then reload.");
    expect(container.querySelector(".feed-row")!.textContent).toContain("Still here");
    expect(container.querySelector(".pl-empty")).toBeNull();
  });
});

// #5094: the scheduler prepends its internal "[Autonomous wake — …]" header to a
// scheduled/watch/wait/goal prompt. The stripping lives in the feed model; the meta
// line ("in response to …") must show only the job's own words.
describe("ActivitySurface — wake-header stripping (#5094)", () => {
  const WAKE = "[Autonomous wake — scheduled run. Orient from <working_state>, then:]";

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

  it("renders a persisted scheduled turn's meta without the header", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [] });
    vi.spyOn(api, "activity").mockResolvedValue({
      ...NO_ACTIVITY,
      entries: [entry({ stimulus: `${WAKE}\n\nCheck the board for blocked cards.` })],
    });

    renderSurface();
    await flush();

    expect(container.querySelector(".feed-row-meta")!.textContent)
      .toBe("Scheduled · heartbeat · in response to “Check the board for blocked cards.”");
    expect(container.textContent).not.toContain("Autonomous wake");
  });

  it("strips a live bus turn's header too, and omits the clause when the stimulus was only the header", async () => {
    vi.spyOn(api, "inbox").mockResolvedValue({ items: [] });
    vi.spyOn(api, "activity").mockResolvedValue({ ...NO_ACTIVITY, entries: [entry({ stimulus: WAKE })] });

    renderSurface();
    await flush();
    // Header-only stimulus: the meta is just the source label.
    expect(container.querySelector(".feed-row-meta")!.textContent).toBe("Scheduled · heartbeat");

    emit("activity.message", {
      text: "done", origin: "scheduler", trigger: "heartbeat",
      stimulus: `${WAKE}\n\nHourly heartbeat check.`,
    });
    expect(container.querySelectorAll(".feed-row-meta")[0].textContent)
      .toBe("Scheduled · heartbeat · in response to “Hourly heartbeat check.”");
    expect(container.textContent).not.toContain("Autonomous wake");
  });
});
