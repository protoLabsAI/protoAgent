import { describe, expect, it } from "vitest";

import { composerPlaceholder } from "./composerPlaceholder";

describe("composerPlaceholder", () => {
  it("hints ↑-recall while streaming with a steer queued (#2837)", () => {
    expect(composerPlaceholder("streaming", 1)).toBe("Press ↑ to edit queued message");
    expect(composerPlaceholder("streaming", 3)).toBe("Press ↑ to edit queued message");
  });

  it("keeps the steer prompt while streaming with nothing queued", () => {
    expect(composerPlaceholder("streaming", 0)).toBe("Steer the agent…");
  });

  it("keeps the idle prompt regardless of leftover queue state", () => {
    expect(composerPlaceholder("idle", 0)).toBe("Message protoAgent…");
    // A queue is only actionable mid-turn; idle/error never show the steer hints.
    expect(composerPlaceholder("idle", 2)).toBe("Message protoAgent…");
    expect(composerPlaceholder("error", 1)).toBe("Message protoAgent…");
  });

  it("keeps the e2e anchors matchable (chat-steer-cancel.spec.ts locators)", () => {
    // The spec grabs these BEFORE a steer is queued — the strings must keep matching.
    expect(composerPlaceholder("streaming", 0)).toMatch(/Steer the agent/i);
    expect(composerPlaceholder("idle", 0)).toMatch(/Message protoAgent/i);
  });

  it("flips back to the steer prompt when the queue drains (cancel / turn-end reconcile)", () => {
    expect(composerPlaceholder("streaming", 1)).toBe("Press ↑ to edit queued message");
    expect(composerPlaceholder("streaming", 0)).toBe("Steer the agent…");
  });
});
