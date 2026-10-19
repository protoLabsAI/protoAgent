import { describe, expect, it } from "vitest";

import { ACTIVE_WINDOW_SECONDS, canManage, isActive, lastSeenLabel, scopeAction, scopeBadge } from "./DeviceList";

// #4336 S3 — the badge follows the server's last-seen throttle (300 s), not a 60 s window
// that a device in constant use could only satisfy one minute in five.
describe("device activity", () => {
  const now = 2_000_000;

  it("trusts the server's active flag when present", () => {
    expect(isActive({ active: true, last_seen_at: now - 10_000 }, now)).toBe(true);
    expect(isActive({ active: false, last_seen_at: now - 1 }, now)).toBe(false);
  });

  it("falls back to the throttle window for an older server", () => {
    expect(isActive({ last_seen_at: now - 299 }, now)).toBe(true); // just before a re-stamp
    expect(isActive({ last_seen_at: now - ACTIVE_WINDOW_SECONDS - 1 }, now)).toBe(false);
    expect(isActive({ last_seen_at: null }, now)).toBe(false);
  });

  it("labels what the stamp can actually say", () => {
    expect(lastSeenLabel({ last_seen_at: null }, now)).toBe("never used");
    expect(lastSeenLabel({ last_seen_at: now - 200 }, now)).toBe("in use in the last few minutes");
    expect(lastSeenLabel({ active: false, last_seen_at: now - 30 * 60 }, now)).toBe("last seen 30m ago");
    expect(lastSeenLabel({ last_seen_at: now - 3 * 3600 }, now)).toBe("last seen 3h ago");
    expect(lastSeenLabel({ last_seen_at: now - 2 * 86400 }, now)).toBe("last seen 2d ago");
  });
});

// ADR 0121 D4 — "Limited" vs "Full access", and the Limit / Promote action.
describe("device scope", () => {
  it("badges phones by scope, and leaves agents and old servers alone", () => {
    expect(scopeBadge({ kind: "device", scope: "device" })?.label).toBe("Limited");
    expect(scopeBadge({ kind: "device", scope: "full" })?.label).toBe("Full access");
    expect(scopeBadge({ kind: "agent", scope: "full" })).toBeNull();
    expect(scopeBadge({ kind: "device" })).toBeNull();
  });

  it("offers Limit on a full-access phone and Promote on a limited one", () => {
    expect(scopeAction({ kind: "device", scope: "full" }, false)).toEqual({ label: "Limit", to: "device" });
    expect(scopeAction({ kind: "device", scope: "device" }, false)).toEqual({ label: "Promote", to: "full" });
    expect(scopeAction({ kind: "agent", scope: "full" }, false)).toBeNull();
    expect(scopeAction({ kind: "device" }, false)).toBeNull();
  });

  it("offers neither when this console is itself limited", () => {
    expect(scopeAction({ kind: "device", scope: "full" }, true)).toBeNull();
    expect(scopeAction({ kind: "device", scope: "device" }, true)).toBeNull();
  });

  it("lets a limited console manage only its own row", () => {
    expect(canManage("a", "a", true)).toBe(true);
    expect(canManage("b", "a", true)).toBe(false);
    expect(canManage("b", "", true)).toBe(false);
    expect(canManage("b", "a", false)).toBe(true);
  });
});
