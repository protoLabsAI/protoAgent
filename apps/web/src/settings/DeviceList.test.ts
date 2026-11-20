import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { PairedDevice } from "../lib/api";
// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

import { DeviceList, pushLine, ACTIVE_WINDOW_SECONDS, canManage, expiryLabel, isActive, lastSeenLabel, scopeAction, scopeBadge } from "./DeviceList";

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

// ADR 0121 D5 (#4336 S7.3) — idle expiry.
describe("expiry label", () => {
  const now = 2_000_000;

  it("says an expired device must be removed and paired again", () => {
    expect(expiryLabel({ expired: true, expires_at: now - 10 }, now)).toMatch(/remove it, then pair the phone again/);
  });

  it("counts the days left, rounding up", () => {
    expect(expiryLabel({ expired: false, expires_at: now + 29 * 86400 + 5 }, now)).toBe("expires in 30 days if unused");
    expect(expiryLabel({ expired: false, expires_at: now + 3600 }, now)).toBe("expires within a day if unused");
  });

  it("says nothing for a device that never expires, or an older server", () => {
    expect(expiryLabel({ expired: false, expires_at: null }, now)).toBeNull();
    expect(expiryLabel({}, now)).toBeNull();
  });
});


vi.mock("@protolabsai/ui/overlays", () => ({ useToast: () => vi.fn() }));

describe("push status", () => {
  const now = new Date(2026, 10, 19, 10, 42).getTime() / 1000;
  const clock = new Date(now * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const device: PairedDevice = { id: "phone", name: "Phone", created_at: 1, last_seen_at: null };
  const push: NonNullable<PairedDevice["push"]> = {
    provider: "apple", generation: 1, vapid_version: 1, created_at: 1, renewed_at: 1,
    last_attempt_at: null, last_accepted_at: null, last_error_at: null, last_error: null,
  };

  it.each([
    [{ kind: "agent" }, null],
    [{}, { tone: "neutral", text: "Not reported yet" }],
    [{ push: null, push_renew: "vapid_key" }, { tone: "warning", text: "Renew needed", detail: "This agent changed its notification key." }],
    [{ push: null, push_renew: "handoff" }, { tone: "warning", text: "Renew needed", detail: "This phone moved to a new address, which needs its own notifications." }],
    [{ push: null, push_renew: "idle" }, { tone: "warning", text: "Renew needed", detail: "This device went unused for too long." }],
    [{ push: null, push_renew: null }, { tone: "neutral", text: "Push off" }],
    [{ push }, { tone: "neutral", text: "Push on · Apple" }],
    [{ push: { ...push, last_attempt_at: now - 1, last_accepted_at: now, last_error_at: now - 2 } },
      { tone: "success", text: `Push on · Apple accepted the last alert at ${clock}` }],
    [{ push: { ...push, last_attempt_at: now - 1, last_accepted_at: now - 2, last_error_at: now, last_error: "network" } },
      { tone: "warning", text: "Push on · Apple didn't accept the last alert", detail: "The alert couldn't reach the push service." }],
    [{ push: { ...push, last_attempt_at: now, last_accepted_at: now - 1, last_error_at: now - 2 } },
      { tone: "neutral", text: `Push on · last alert sent at ${clock}` }],
    [{ push: { ...push, last_attempt_at: now, last_accepted_at: now, last_error_at: now } },
      { tone: "success", text: `Push on · Apple accepted the last alert at ${clock}` }],
  ] as [Partial<PairedDevice>, ReturnType<typeof pushLine>][])("reports %j", (fields, expected) => {
    expect(pushLine({ ...device, ...fields }, now)).toEqual(expected);
  });

  it.each([
    ["timeout", "The push service took too long to answer."],
    ["network", "The alert couldn't reach the push service."],
    ["service_retry", "The push service was busy or had a problem."],
    ["rejected", "The push service refused this alert. It won't be sent again."],
    ["retries_exhausted", "protoAgent stopped trying before the push service took the alert."],
    ["future_code", "Error code: future_code"],
  ])("explains %s without guessing retry state", (code, detail) => {
    expect(pushLine({ ...device, push: { ...push, last_attempt_at: now, last_error_at: now, last_error: code } }, now)?.detail).toBe(detail);
  });

  it.each(["google", "mozilla"])("names %s and uses a date for earlier alerts", (provider) => {
    const yesterday = new Date(2026, 10, 18, 10, 42).getTime() / 1000;
    expect(pushLine({ ...device, push: { ...push, provider, last_attempt_at: yesterday, last_accepted_at: yesterday } }, now)?.text)
      .toBe(`Push on · ${provider === "google" ? "Google" : "Mozilla"} accepted the last alert at ${new Date(yesterday * 1000).toLocaleDateString()}`);
  });

  it("renders neutral identity badges and push text with a decorative icon", () => {
    const host = document.createElement("div");
    host.innerHTML = renderToStaticMarkup(createElement(DeviceList, {
      devices: [{ ...device, scope: "device", push: null }, { ...device, id: "agent", kind: "agent" }],
      loading: false, callerDeviceId: device.id, onChanged: () => {},
    }));
    for (const label of ["This device", "Limited", "Agent"]) {
      const badge = Array.from(host.querySelectorAll(".pl-badge")).find((el) => el.textContent === label);
      expect(badge?.className).toBe("pl-badge");
    }
    expect(host.querySelectorAll(".devices-push-line")).toHaveLength(1);
    expect(host.querySelector(".devices-push-line")?.textContent).toBe("Push off");
    expect(host.querySelector(".devices-push-line svg")?.getAttribute("aria-hidden")).toBe("true");
    expect(host.textContent).toContain("Accepted means the push service took the alert. It doesn't mean you saw it.");
  });
});
