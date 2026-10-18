import { describe, expect, it } from "vitest";

import { ACTIVE_WINDOW_SECONDS, isActive, lastSeenLabel } from "./DeviceList";

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
