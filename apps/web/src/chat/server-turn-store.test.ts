import { describe, it, expect, beforeEach } from "vitest";

import {
  labelForOrigin,
  noteTurnFinished,
  noteTurnStarted,
  originForSession,
  rememberOrigin,
  resetServerTurns,
  serverResultLabel,
  serverResultPreview,
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

describe("remembered origin (#3028)", () => {
  beforeEach(() => resetServerTurns());

  it("has no remembered origin for an untouched session", () => {
    expect(originForSession("s1")).toBe("");
  });

  it("remembers the raw origin captured at turn.started", () => {
    rememberOrigin("s1", "scheduler");
    expect(originForSession("s1")).toBe("scheduler");
  });

  it("OUTLIVES turn.finished so the terminal chat.resumed can still read it", () => {
    // The whole reason it's kept separately from the ref-counted indicator: `chat.resumed`
    // (which tags the settled result card) can land after the indicator has cleared.
    rememberOrigin("s1", "watch-abc");
    noteTurnStarted("s1", labelForOrigin("watch-abc"));
    noteTurnFinished("s1");
    expect(serverTurnLabel("s1")).toBeNull(); // indicator gone…
    expect(originForSession("s1")).toBe("watch-abc"); // …but the origin remains for tagging
  });

  it("ignores an empty session id or empty origin", () => {
    rememberOrigin("", "scheduler");
    rememberOrigin("s1", "");
    expect(originForSession("")).toBe("");
    expect(originForSession("s1")).toBe("");
  });

  it("is cleared by resetServerTurns", () => {
    rememberOrigin("s1", "scheduler");
    resetServerTurns();
    expect(originForSession("s1")).toBe("");
  });
});

describe("serverResultLabel (compact result card, #3028)", () => {
  it("gives each server origin a short noun label", () => {
    expect(serverResultLabel("scheduler")).toBe("Scheduled task");
    expect(serverResultLabel("background-resume")).toBe("Background report");
    expect(serverResultLabel("background")).toBe("Background task");
    expect(serverResultLabel("inbox")).toBe("Inbox message");
    expect(serverResultLabel("webhook")).toBe("Webhook trigger");
  });

  it("labels a watch reaction from its watch-<id> origin", () => {
    expect(serverResultLabel("watch")).toBe("Watch trigger");
    expect(serverResultLabel("watch-9f3")).toBe("Watch trigger");
  });

  it("labels an unknown server origin as an autonomous run", () => {
    expect(serverResultLabel("something-new")).toBe("Autonomous run");
  });

  it("returns null for an empty origin (an operator turn — never a card)", () => {
    expect(serverResultLabel("")).toBeNull();
    expect(serverResultLabel("   ")).toBeNull();
  });
});

describe("serverResultPreview (collapsed summary line, #3028)", () => {
  it("flattens whitespace and keeps short content whole", () => {
    expect(serverResultPreview("Ball secured\nat (8,17).")).toBe("Ball secured at (8,17).");
  });

  it("clips long content to ~120 chars with an ellipsis", () => {
    const long = "x".repeat(200);
    const preview = serverResultPreview(long);
    expect(preview.endsWith("…")).toBe(true);
    expect(preview.length).toBeLessThanOrEqual(121); // 120 chars + the ellipsis
  });

  it("prefers a leading `## Summary` section's body when present", () => {
    const md = "Some preamble.\n\n## Summary\nThe deploy went green.\n\n## Details\nlots of noise here";
    expect(serverResultPreview(md)).toBe("The deploy went green.");
  });

  it("tolerates a summary heading with a trailing colon and other heading levels", () => {
    expect(serverResultPreview("# Summary:\nAll clear.\n# Next\nmore")).toBe("All clear.");
  });

  it("falls back to the head of the content when there is no summary section", () => {
    expect(serverResultPreview("No heading here, just text.")).toBe("No heading here, just text.");
  });
});
