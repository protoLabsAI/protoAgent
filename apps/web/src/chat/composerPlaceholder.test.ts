// @vitest-environment node
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

  it("names the agent in the idle prompt", () => {
    expect(composerPlaceholder("idle", 0, "brandAgent")).toBe("Message brandAgent…");
    // A queue is only actionable mid-turn; idle/error never show the steer hints.
    expect(composerPlaceholder("idle", 2, "protoAgent")).toBe("Message protoAgent…");
    expect(composerPlaceholder("error", 1, "Gina")).toBe("Message Gina…");
  });

  it("falls back to a generic prompt when no name is known", () => {
    expect(composerPlaceholder("idle", 0)).toBe("Message the agent…");
    expect(composerPlaceholder("idle", 0, "")).toBe("Message the agent…");
    expect(composerPlaceholder("idle", 0, "   ")).toBe("Message the agent…");
    expect(composerPlaceholder("idle", 0, null)).toBe("Message the agent…");
  });

  it("ignores the name while streaming — the steer states are unchanged", () => {
    expect(composerPlaceholder("streaming", 0, "brandAgent")).toBe("Steer the agent…");
    expect(composerPlaceholder("streaming", 1, "brandAgent")).toBe("Press ↑ to edit queued message");
  });

  it("keeps the e2e anchors matchable (chat-steer-cancel.spec.ts locators)", () => {
    // The spec grabs these BEFORE a steer is queued — the strings must keep matching.
    // The idle anchor is the branded agent name; the e2e runtime reports "protoAgent".
    expect(composerPlaceholder("streaming", 0)).toMatch(/Steer the agent/i);
    expect(composerPlaceholder("idle", 0, "protoAgent")).toMatch(/Message protoAgent/i);
  });

  it("flips back to the steer prompt when the queue drains (cancel / turn-end reconcile)", () => {
    expect(composerPlaceholder("streaming", 1)).toBe("Press ↑ to edit queued message");
    expect(composerPlaceholder("streaming", 0)).toBe("Steer the agent…");
  });
});
