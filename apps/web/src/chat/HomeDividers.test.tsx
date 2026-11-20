// Home's dividers and announcer (#5545): day labels ("Today" / "Yesterday" / the date), the
// "N new" line above the first entry newer than last_read_at, and the one polite status line
// that names each entry arriving while Home is open. Mounted with createRoot/act.
import { act, createElement as h } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../lib/types";

vi.mock("../app/NoticeClickWatch", () => ({ openNotice: vi.fn(async () => true) }));

import {
  dayLabel,
  HomeAnnouncer,
  HomeDivider,
  homeDividers,
  homeEntryTitle,
  homeUnreadCount,
  useHomeNewAnchor,
  useHomeToday,
} from "./HomeDividers";

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Local-time instants, so the suite reads the same in any timezone.
const at = (day: number, hour: number, minute = 0) => new Date(2026, 10, day, hour, minute).getTime();
const iso = (ms: number) => new Date(ms).toISOString();
const brief = (id: string, createdAt: number | undefined, content = `Brief ${id}`): ChatMessage =>
  ({ id, role: "assistant", content, status: "done", createdAt });
const own = (id: string, createdAt: number): ChatMessage => ({ id, role: "user", content: "mine", status: "done", createdAt });
const alert = (id: string, createdAt: number, title: string): ChatMessage => ({
  id, role: "user", status: "done", createdAt,
  content: `[Home entry: notify from fleet-health, importance high. For reference; not a request.]\n${title}\n\nDetails.`,
  home: { kind: "notify", importance: "high", source: "fleet-health", notice_id: id },
});

let container: HTMLElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
});

describe("day labels", () => {
  it("names calendar days, not 24-hour windows, on both sides of midnight", () => {
    const now = at(19, 0, 5);
    expect(dayLabel(at(19, 0, 2), now)).toBe("Today");
    expect(dayLabel(at(18, 23, 58), now)).toBe("Yesterday"); // 7 minutes ago, but yesterday
    expect(dayLabel(at(18, 0, 1), now)).toBe("Yesterday");
    const older = dayLabel(at(17, 23, 59), now);
    expect(older).toBe(new Date(at(17, 12)).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }));
    expect(older).not.toMatch(/2026/);
  });

  it("crosses a month and a year boundary, adding the year only when it differs", () => {
    const newYear = new Date(2027, 0, 1, 0, 1).getTime();
    expect(dayLabel(new Date(2026, 11, 31, 23, 59).getTime(), newYear)).toBe("Yesterday");
    expect(dayLabel(new Date(2026, 11, 30, 12).getTime(), newYear)).toMatch(/2026/);
  });

  it("puts one label above the first message of each day and none on untimed messages", () => {
    const messages = [
      brief("a", at(10, 9)), brief("b", at(18, 23, 58)), brief("c", at(18, 23, 59)),
      brief("d", at(19, 0, 2)), brief("untimed", undefined), own("e", at(19, 0, 3)),
    ];
    const slots = homeDividers(messages, { lastReadAt: iso(at(19, 1)), now: at(19, 0, 5) });
    expect(slots.map((slot) => slot?.day ?? null)).toEqual([dayLabel(at(10, 9), at(19, 0, 5)), "Yesterday", null, "Today", null, null]);
    // The same transcript read a day later: every label has moved on.
    const later = homeDividers(messages, { lastReadAt: iso(at(19, 1)), now: at(20, 0, 5) });
    expect(later[1]?.day).toBe(dayLabel(at(18, 12), at(20, 12)));
    expect(later[1]?.day).not.toBe("Yesterday");
    expect(later[3]?.day).toBe("Yesterday");
  });

  it("re-reads the clock when the local day rolls over", () => {
    vi.useFakeTimers();
    vi.setSystemTime(at(18, 23, 58));
    const sent = at(18, 23, 50);
    function Probe({ enabled }: { enabled: boolean }) {
      return h("span", null, dayLabel(sent, useHomeToday(enabled)));
    }
    act(() => root.render(h(Probe, { enabled: true })));
    expect(container.textContent).toBe("Today");
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(container.textContent).toBe("Today"); // 23:59 — still the same day
    act(() => { vi.advanceTimersByTime(3 * 60_000); });
    expect(container.textContent).toBe("Yesterday");
    // …and it re-arms for the next midnight.
    act(() => { vi.advanceTimersByTime(24 * 60 * 60_000); });
    expect(container.textContent).not.toBe("Yesterday");
  });

  it("arms no timer for a session that is not Home", () => {
    vi.useFakeTimers();
    function Probe() { useHomeToday(false); return null; }
    act(() => root.render(h(Probe)));
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('"N new" divider', () => {
  const readAt = at(19, 9);
  const messages = [
    brief("old", at(19, 8)), own("mine-late", at(19, 9, 30)), brief("exact", readAt),
    alert("n1", at(19, 10), "Fleet member sol stopped"), own("mine", at(19, 11)), brief("n2", at(19, 12)), brief("n3", at(19, 13)),
  ];

  it("sits above the first entry newer than last_read_at and counts every such entry", () => {
    const slots = homeDividers(messages, { lastReadAt: iso(readAt), now: at(19, 14) });
    // Not the operator's own later send, not the entry written exactly at last_read_at.
    expect(slots.map((slot) => slot?.fresh ?? null)).toEqual([null, null, null, 3, null, null, null]);
    expect(homeUnreadCount(messages, iso(readAt))).toBe(3);
  });

  it("renders as plain muted text directly above that entry, below its day label", () => {
    const list = [brief("old", at(18, 8)), ...messages.slice(3)];
    const slots = homeDividers(list, { lastReadAt: iso(readAt), now: at(19, 14) });
    act(() => root.render(h("div", null, list.flatMap((message, index) => [
      slots[index] ? h(HomeDivider, { key: `d-${message.id}`, slot: slots[index]! }) : null,
      h("div", { key: message.id, "data-row": message.id }),
    ]))));
    const order = [...container.firstElementChild!.children].map((el) => el.getAttribute("data-row") ?? el.textContent);
    expect(order).toEqual(["Yesterday", "old", "Today", "3 new", "n1", "mine", "n2", "n3"]);
    for (const line of container.querySelectorAll(".pl-home-divider")) {
      expect(line.tagName).toBe("P");
      expect(line.getAttribute("role")).toBeNull();
      expect(line.className).not.toMatch(/pl-message|pl-divider/);
    }
  });

  it("shows nothing when everything is read, and counts everything when Home was never read", () => {
    expect(homeDividers(messages, { lastReadAt: iso(at(19, 13)), now: at(19, 14) }).some((slot) => slot?.fresh)).toBe(false);
    const never = homeDividers(messages, { lastReadAt: null, now: at(19, 14) });
    expect(never[0]?.fresh).toBe(5);
    expect(homeUnreadCount(messages, undefined)).toBe(5);
  });

  it("holds its place while Home stays open, though opening Home marks it read", () => {
    const seen: (string | null | undefined)[] = [];
    function Probe({ open, lastReadAt }: { open: boolean; lastReadAt: string | null }) {
      seen.push(useHomeNewAnchor(open, lastReadAt));
      return null;
    }
    const render = (open: boolean, lastReadAt: string | null) => act(() => root.render(h(Probe, { open, lastReadAt })));
    render(false, "t1");
    render(false, "t2"); // closed: follows the server
    render(true, "t2");
    render(true, "t3"); // mark-read answered: still measured from where it was on opening
    render(false, "t3");
    render(true, "t3"); // reopened: measured afresh
    expect(seen).toEqual(["t1", "t2", "t2", "t2", "t3", "t3"]);
  });
});

describe("HomeAnnouncer", () => {
  const OPENED = at(19, 9);
  const status = () => container.querySelector('[role="status"]')!;
  const said = () => status().textContent!.trim();
  const render = (messages: ChatMessage[], open = true) => act(() => root.render(h(HomeAnnouncer, { messages, open })));

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(OPENED);
  });

  it("is one polite, atomic, visually hidden status line", () => {
    render([]);
    expect(container.querySelectorAll("[aria-live]")).toHaveLength(1);
    expect(status().getAttribute("aria-live")).toBe("polite");
    expect(status().getAttribute("aria-atomic")).toBe("true");
    expect(status().className).toBe("pl-home-announce");
    expect(said()).toBe("");
  });

  it("announces each entry that arrives while Home is open exactly once", () => {
    const history = [brief("h1", OPENED - 60_000), alert("h2", OPENED - 30_000, "Old alert")];
    render(history);
    expect(said()).toBe(""); // what was already there is not news
    const first = alert("n1", OPENED + 1000, "Fleet member sol stopped");
    render([...history, first]);
    expect(said()).toBe("New in Home: Fleet member sol stopped");
    const before = status().textContent;
    render([...history, { ...first }]); // an unrelated re-render with fresh objects
    expect(status().textContent).toBe(before);
    render([...history, first, brief("n2", OPENED + 2000, "## Morning brief\n\nThree things.")]);
    expect(said()).toBe("New in Home: Morning brief");
  });

  it("makes a repeat of the same title a fresh announcement", () => {
    render([]);
    render([alert("n1", OPENED + 1000, "Disk almost full")]);
    const first = status().textContent;
    render([alert("n1", OPENED + 1000, "Disk almost full"), alert("n2", OPENED + 2000, "Disk almost full")]);
    expect(status().textContent).not.toBe(first);
    expect(said()).toBe("New in Home: Disk almost full");
  });

  it("never announces the operator's own sends", () => {
    render([]);
    render([own("u1", OPENED + 1000)]);
    expect(said()).toBe("");
  });

  it("announces nothing while Home is not open, nor on reopening for what arrived meanwhile", () => {
    render([], false);
    const arrived = [alert("n1", OPENED + 1000, "Arrived while closed")];
    render(arrived, false);
    expect(said()).toBe("");
    vi.setSystemTime(OPENED + 5000);
    render(arrived, true);
    expect(said()).toBe("");
    render([...arrived, alert("n2", OPENED + 6000, "Arrived while open")], true);
    expect(said()).toBe("New in Home: Arrived while open");
    render([...arrived, alert("n2", OPENED + 6000, "Arrived while open")], false);
    expect(said()).toBe(""); // closing clears the line, so reopening cannot replay it
  });

  it("stays silent for history that loads after opening and for entries with no time", () => {
    render([]);
    render([brief("h1", OPENED - 86_400_000), alert("h2", OPENED - 1, "Yesterday's alert"), brief("h3", undefined)]);
    expect(said()).toBe("");
  });

  it("waits for a streaming message to settle, then names it once — even if re-read under a new id", () => {
    render([]);
    const live: ChatMessage = { id: "live", role: "assistant", content: "Res", status: "streaming", createdAt: OPENED + 1000 };
    render([live]);
    render([{ ...live, content: "Result: 4 jobs" }]);
    expect(said()).toBe(""); // no streamed token is ever read out
    const settled = { ...live, content: "Result: 4 jobs ran", status: "done" as const, createdAt: OPENED + 3000 };
    render([settled]);
    expect(said()).toBe("New in Home: Result: 4 jobs ran");
    const before = status().textContent;
    render([{ ...settled, id: "server-id" }]);
    expect(status().textContent).toBe(before);
  });

  it("titles an entry from its card title or first line, bounded", () => {
    expect(homeEntryTitle(alert("a", 1, "Spend cap reached"))).toBe("Spend cap reached");
    expect(homeEntryTitle(brief("b", 1, "\n\n**Done** — `3` files\nmore"))).toBe("Done — 3 files");
    expect(homeEntryTitle(brief("c", 1, ""))).toBe("new entry");
    expect(homeEntryTitle(brief("d", 1, "x".repeat(400))).length).toBe(120);
  });
});
