import { describe, expect, it } from "vitest";

import type { ActivityEntry, InboxItem } from "../lib/types";
import { applyLiveEntry, groupByDay, mergeFeed } from "./feedModel";

const WAKE = "[Autonomous wake — scheduled run. Orient from <working_state>, then:]";

// Local-time ISO stamps so day grouping (local) and parsing (UTC) agree in any zone.
const at = (d: number, h = 9) => new Date(2026, 10, d, h).toISOString();

const item = (over: Partial<InboxItem> = {}): InboxItem => ({
  id: 1, created_at: at(12, 10), priority: "next", source: "github",
  text: "Build failed on main", dedup_key: null, delivered_at: null, ...over,
});

const entry = (over: Partial<ActivityEntry> = {}): ActivityEntry => ({
  id: 10, created_at: at(12, 11), origin: "scheduler", trigger: "ds-drift-watch",
  priority: "", state: "completed", text: "Checked the dashboards.\nNo drift.",
  task_id: "t1", ...over,
});

describe("mergeFeed", () => {
  it("shows a pending item as one queued row with the runs-when-free meta", () => {
    const rows = mergeFeed([item()], [], "roxy");
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("queued");
    expect(rows[0].title).toBe("Build failed on main");
    expect(rows[0].meta).toBe("Inbox · github · Queued — runs when roxy is free");
  });

  it("never shows a delivered item", () => {
    expect(mergeFeed([item({ delivered_at: at(12, 11) })], [], "roxy")).toHaveLength(0);
  });

  it("shows the entry, not the item, when inbox_id claims it — exactly one row", () => {
    const rows = mergeFeed([item()], [entry({ inbox_id: 1, origin: "inbox", trigger: "github" })], "roxy");
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe("ran");
    expect(rows[0].title).toBe("Checked the dashboards.");
    expect(rows[0].entry?.id).toBe(10);
  });

  it("marks a failed entry, sorts newest first, and keeps unrelated items", () => {
    const rows = mergeFeed(
      [item({ id: 2, created_at: at(12, 12), text: "Deploy the docs" })],
      [entry({ id: 11, created_at: at(12, 13), state: "failed" }), entry()],
      "roxy",
    );
    expect(rows.map((r) => r.kind)).toEqual(["failed", "queued", "ran"]);
    expect(rows.map((r) => r.key)).toEqual(["entry:11", "item:2", "entry:10"]);
  });

  it("builds the source label from origin + trigger, omitting the stimulus clause when empty", () => {
    const rows = mergeFeed(
      [],
      [
        entry({ id: 1, stimulus: `${WAKE}\nCheck drift.` }),
        entry({ id: 2, origin: "a2a", trigger: "protoEngineer", stimulus: "ping" }),
        entry({ id: 3, origin: "operator", trigger: "" }),
      ],
      "roxy",
    );
    expect(rows.map((r) => r.meta)).toEqual([
      "Scheduled · ds-drift-watch · in response to “Check drift.”",
      "Another agent · protoEngineer · in response to “ping”",
      "You",
    ]);
  });

  it("strips the wake header from a queued title too", () => {
    const rows = mergeFeed([item({ text: `${WAKE}\nCheck the board.` })], [], "roxy");
    expect(rows[0].title).toBe("Check the board.");
  });
});

describe("applyLiveEntry", () => {
  const queuedAmongOthers = () =>
    mergeFeed([item()], [entry({ id: 99, created_at: at(12, 12), inbox_id: null })], "roxy");

  it("replaces the matching queued row at the same index, idempotently", () => {
    const rows = queuedAmongOthers();
    expect(rows.map((r) => r.kind)).toEqual(["ran", "queued"]);
    const live = entry({ id: 10, inbox_id: 1, origin: "inbox", trigger: "github" });
    const next = applyLiveEntry(rows, live, "roxy");
    expect(next.map((r) => r.key)).toEqual(["entry:99", "entry:10"]);
    expect(next[1].kind).toBe("ran");
    expect(applyLiveEntry(next, live, "roxy")).toBe(next);
  });

  it("prepends an entry with no matching queued row", () => {
    const rows = queuedAmongOthers();
    const next = applyLiveEntry(rows, entry({ id: 10, inbox_id: 7, state: "failed" }), "roxy");
    expect(next).toHaveLength(3);
    expect(next[0]).toMatchObject({ key: "entry:10", kind: "failed" });
  });
});

describe("groupByDay", () => {
  it("labels Today, Yesterday, then the short date, in list order", () => {
    const rows = mergeFeed(
      [],
      [
        entry({ id: 1, created_at: at(12) }),
        entry({ id: 2, created_at: at(11) }),
        entry({ id: 3, created_at: at(10) }),
        entry({ id: 4, created_at: at(10, 8) }),
      ],
      "roxy",
    );
    const groups = groupByDay(rows, new Date(2026, 10, 12, 15));
    expect(groups.map((g) => [g.heading, g.rows.length])).toEqual([
      ["Today", 1],
      ["Yesterday", 1],
      ["Nov 10", 2],
    ]);
  });
});
