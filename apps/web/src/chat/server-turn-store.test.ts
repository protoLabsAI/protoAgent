import { describe, it, expect, beforeEach } from "vitest";

import {
  labelForOrigin,
  noteTurnFinished,
  noteTurnStarted,
  resetServerTurns,
  serverTurnLabel,
  serverTurnSessionsKey,
} from "./server-turn-store";

// The server-turn store powers the #1767 typing indicator: `turn.started` arms a labelled
// indicator for a session, `turn.finished` clears it. It ref-counts so overlapping turns
// into one session don't clear the indicator early.

describe("labelForOrigin", () => {
  it("maps the fixed backend origins to human phrasings", () => {
    expect(labelForOrigin("background-resume")).toMatch(/background report/i);
    expect(labelForOrigin("scheduler")).toMatch(/scheduled/i);
  });

  it("recognises a watch reaction from its watch-<id> origin", () => {
    expect(labelForOrigin("watch-abc123")).toMatch(/watch/i);
    expect(labelForOrigin("watch")).toMatch(/watch/i);
  });

  it("falls back to a generic label for an unknown origin", () => {
    const label = labelForOrigin("something-new");
    expect(label).toBeTruthy();
    expect(label).not.toContain("something-new"); // never leaks a raw token to the operator
  });
});

describe("server-turn store", () => {
  beforeEach(() => resetServerTurns());

  it("has no label for a session with no server turn", () => {
    expect(serverTurnLabel("s1")).toBeNull();
  });

  it("arms the labelled indicator on turn.started and clears it on turn.finished", () => {
    noteTurnStarted("s1", labelForOrigin("scheduler"));
    expect(serverTurnLabel("s1")).toMatch(/scheduled/i);
    noteTurnFinished("s1");
    expect(serverTurnLabel("s1")).toBeNull();
  });

  it("keeps the indicator until the LAST overlapping turn finishes (ref-counted)", () => {
    noteTurnStarted("s1", labelForOrigin("background-resume"));
    noteTurnStarted("s1", labelForOrigin("background-resume"));
    noteTurnFinished("s1");
    expect(serverTurnLabel("s1")).toMatch(/background report/i); // one still in flight
    noteTurnFinished("s1");
    expect(serverTurnLabel("s1")).toBeNull();
  });

  it("scopes the indicator to its own session", () => {
    noteTurnStarted("s1", labelForOrigin("scheduler"));
    expect(serverTurnLabel("s2")).toBeNull();
    noteTurnFinished("s1");
  });

  it("ignores an empty session id", () => {
    noteTurnStarted("", "x");
    expect(serverTurnLabel("")).toBeNull();
  });

  it("never underflows when finish arrives without a matching start", () => {
    noteTurnFinished("s1"); // stray finish (e.g. a replayed frame)
    expect(serverTurnLabel("s1")).toBeNull();
    noteTurnStarted("s1", labelForOrigin("scheduler"));
    expect(serverTurnLabel("s1")).toMatch(/scheduled/i); // start still arms cleanly
  });
});

describe("serverTurnSessionsKey (per-tab processing indicator, #2009)", () => {
  beforeEach(() => resetServerTurns());

  it("is empty with no server turns in flight", () => {
    expect(serverTurnSessionsKey()).toBe("");
  });

  it("lists every session with a turn in flight, stably sorted", () => {
    noteTurnStarted("s2", "x");
    noteTurnStarted("s1", "x");
    expect(serverTurnSessionsKey()).toBe("s1,s2"); // sorted → stable snapshot regardless of arrival order
  });

  it("drops a session only when its LAST overlapping turn finishes (no flicker)", () => {
    noteTurnStarted("s1", "x");
    noteTurnStarted("s1", "x"); // two overlapping turns on one session
    noteTurnFinished("s1");
    expect(serverTurnSessionsKey()).toBe("s1"); // still processing — one in flight
    noteTurnFinished("s1");
    expect(serverTurnSessionsKey()).toBe(""); // clears only when the last settles
  });

  it("keeps the same key when an unrelated session churns (Object.is-stable snapshot)", () => {
    noteTurnStarted("s1", "x");
    const before = serverTurnSessionsKey();
    noteTurnStarted("s2", "x");
    noteTurnFinished("s2");
    expect(serverTurnSessionsKey()).toBe(before); // back to just "s1" — no re-render churn for s1's tab
  });
});
