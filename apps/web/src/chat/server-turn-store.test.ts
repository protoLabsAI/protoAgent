import { describe, it, expect, beforeEach } from "vitest";

import {
  labelForOrigin,
  noteTurnFinished,
  noteTurnStarted,
  resetServerTurns,
  serverTurnLabel,
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
