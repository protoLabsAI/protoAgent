import { describe, expect, it } from "vitest";

import {
  checkOpenLink,
  createFrameBridge,
  OPEN_LINK_FEATURES,
  SEND_MAX_CHARS,
  SEND_RATE_LIMIT_MS,
  type SendCheck,
} from "./frameBridge";

// ADR 0118 D4 — the send-to-chat / openLink bridge gates, enforced in the console host. The
// gates themselves are pure; a mutable `clock` drives the injected `now` so the per-frame rate
// window is deterministic without fake timers.

/** A send check that passes every gate but the one under test (valid text, real gesture, idle). */
function sendCheck(over: Partial<SendCheck> = {}): SendCheck {
  return {
    frameId: "frame-1",
    text: "Tell me more about this scenario",
    userActivation: { isActive: true },
    isBusy: () => false,
    ...over,
  };
}

describe("send gate", () => {
  it("accepts a valid send from a real user gesture", () => {
    const bridge = createFrameBridge({ now: () => 0 });
    const v = bridge.checkSend(sendCheck());
    expect(v).toEqual({ status: "ok", text: "Tell me more about this scenario" });
  });

  it("rejects a send with no user activation (API present, gesture inactive)", () => {
    const bridge = createFrameBridge({ now: () => 0 });
    const v = bridge.checkSend(sendCheck({ userActivation: { isActive: false } }));
    expect(v.status).toBe("rejected");
    if (v.status === "rejected") expect(v.reason).toBe("no-gesture");
  });

  it("asks for confirmation when the userActivation API is missing", () => {
    const bridge = createFrameBridge({ now: () => 0 });
    // The host passes its own navigator.userActivation, which is undefined on this runtime.
    const v = bridge.checkSend(sendCheck({ userActivation: undefined, text: "Run it again" }));
    expect(v.status).toBe("needs-confirm");
    if (v.status === "needs-confirm") {
      expect(v.text).toBe("Run it again");
      expect(v.message).toContain("Run it again");
      expect(v.message).toContain("chat?");
    }
    // A null userActivation (also "API missing") behaves the same.
    const v2 = bridge.checkSend(sendCheck({ userActivation: null }));
    expect(v2.status).toBe("needs-confirm");
  });

  it("rejects an empty or whitespace-only send", () => {
    const bridge = createFrameBridge({ now: () => 0 });
    for (const text of ["", "   ", "\n\t "]) {
      const v = bridge.checkSend(sendCheck({ text }));
      expect(v.status).toBe("rejected");
      if (v.status === "rejected") expect(v.reason).toBe("empty");
    }
  });

  it("rejects a send longer than the 4000-char limit, accepts one at the limit", () => {
    const bridge = createFrameBridge({ now: () => 0 });
    const tooLong = bridge.checkSend(sendCheck({ text: "x".repeat(SEND_MAX_CHARS + 1) }));
    expect(tooLong.status).toBe("rejected");
    if (tooLong.status === "rejected") expect(tooLong.reason).toBe("too-long");

    const atLimit = bridge.checkSend(sendCheck({ frameId: "frame-at-limit", text: "x".repeat(SEND_MAX_CHARS) }));
    expect(atLimit.status).toBe("ok");
  });

  it('rejects a send while the agent is busy, with the exact "the agent is busy" message', () => {
    const bridge = createFrameBridge({ now: () => 0 });
    const v = bridge.checkSend(sendCheck({ isBusy: () => true }));
    expect(v.status).toBe("rejected");
    if (v.status === "rejected") {
      expect(v.reason).toBe("busy");
      expect(v.message).toBe("the agent is busy");
    }
  });

  it("rejects a second send within 2 s from the same frame, then allows one after the window", () => {
    let clock = 1000;
    const bridge = createFrameBridge({ now: () => clock });

    expect(bridge.checkSend(sendCheck()).status).toBe("ok"); // reserves the window at t=1000

    clock = 1000 + SEND_RATE_LIMIT_MS - 1; // still inside the 2 s window
    const tooSoon = bridge.checkSend(sendCheck());
    expect(tooSoon.status).toBe("rejected");
    if (tooSoon.status === "rejected") expect(tooSoon.reason).toBe("rate-limited");

    clock = 1000 + SEND_RATE_LIMIT_MS; // window elapsed
    expect(bridge.checkSend(sendCheck()).status).toBe("ok");
  });

  it("rate-limits per frame — a different frame is not blocked by another's send", () => {
    let clock = 0;
    const bridge = createFrameBridge({ now: () => clock });
    expect(bridge.checkSend(sendCheck({ frameId: "a" })).status).toBe("ok");
    clock = 500; // well inside the window
    expect(bridge.checkSend(sendCheck({ frameId: "b" })).status).toBe("ok");
    expect(bridge.checkSend(sendCheck({ frameId: "a" })).status).toBe("rejected");
  });

  // ADR 0118 S16 / #4122 — a gesture is trusted only when it landed INSIDE this frame. The host
  // injects `focusInFrame` (document.activeElement === its iframe); `userActivation.isActive` is
  // true for a click ANYWHERE on the page, so without this a click on console chrome or one meant
  // for a sibling frame could drive this frame's send.
  it("rejects an active gesture whose focus is not in the frame (#4122)", () => {
    const bridge = createFrameBridge({ now: () => 0 });
    const v = bridge.checkSend(sendCheck({ userActivation: { isActive: true }, focusInFrame: () => false }));
    expect(v.status).toBe("rejected");
    if (v.status === "rejected") expect(v.reason).toBe("no-gesture");
  });

  it("accepts an active gesture whose focus IS in the frame", () => {
    const bridge = createFrameBridge({ now: () => 0 });
    const v = bridge.checkSend(sendCheck({ userActivation: { isActive: true }, focusInFrame: () => true }));
    expect(v.status).toBe("ok");
  });

  it("focus-in-frame is moot when the UA API is missing — still needs-confirm", () => {
    const bridge = createFrameBridge({ now: () => 0 });
    // The operator confirms explicitly on this path, so an unfocused frame still asks rather than
    // rejecting (and never advances the rate window).
    const v = bridge.checkSend(sendCheck({ userActivation: undefined, focusInFrame: () => false }));
    expect(v.status).toBe("needs-confirm");
  });

  it("a focus-rejected send never reserves the rate window", () => {
    let clock = 0;
    const bridge = createFrameBridge({ now: () => clock });
    expect(bridge.checkSend(sendCheck({ focusInFrame: () => false })).status).toBe("rejected");
    // A later focused send from the same frame is NOT rate-limited — the rejected one reserved nothing.
    clock = 10;
    expect(bridge.checkSend(sendCheck({ focusInFrame: () => true })).status).toBe("ok");
  });

  it("noteSend reserves the window for the needs-confirm path", () => {
    let clock = 0;
    const bridge = createFrameBridge({ now: () => clock });
    // A needs-confirm verdict does NOT reserve the window (the send hasn't happened yet)...
    expect(bridge.checkSend(sendCheck({ userActivation: undefined })).status).toBe("needs-confirm");
    // ...so the host advances it itself once the user confirms and the message is posted.
    bridge.noteSend("frame-1");
    clock = SEND_RATE_LIMIT_MS - 1;
    const v = bridge.checkSend(sendCheck());
    expect(v.status).toBe("rejected");
    if (v.status === "rejected") expect(v.reason).toBe("rate-limited");
  });
});

describe("openLink gate", () => {
  it("accepts an https link with no allowlist, carrying noopener,noreferrer", () => {
    const v = checkOpenLink({ url: "https://example.com/docs?q=1" });
    expect(v.status).toBe("ok");
    if (v.status === "ok") {
      expect(v.url).toBe("https://example.com/docs?q=1");
      expect(v.features).toBe(OPEN_LINK_FEATURES);
      expect(v.features).toBe("noopener,noreferrer");
    }
  });

  it("rejects a non-https link", () => {
    for (const url of ["http://example.com", "ftp://example.com/f", "javascript:alert(1)", "data:text/html,x"]) {
      const v = checkOpenLink({ url });
      expect(v.status).toBe("rejected");
      if (v.status === "rejected") expect(["not-https", "invalid-url"]).toContain(v.reason);
    }
    // http specifically reports not-https (it IS a valid URL, just the wrong scheme).
    const http = checkOpenLink({ url: "http://example.com" });
    if (http.status === "rejected") expect(http.reason).toBe("not-https");
  });

  it("rejects a malformed URL", () => {
    const v = checkOpenLink({ url: "not a url" });
    expect(v.status).toBe("rejected");
    if (v.status === "rejected") expect(v.reason).toBe("invalid-url");
  });

  it("allows an origin on the allowlist and rejects one outside it", () => {
    const allowOrigins = ["https://good.example.com", "https://also-good.test"];
    const allowed = checkOpenLink({ url: "https://good.example.com/path", allowOrigins });
    expect(allowed.status).toBe("ok");

    const blocked = checkOpenLink({ url: "https://evil.example.com/path", allowOrigins });
    expect(blocked.status).toBe("rejected");
    if (blocked.status === "rejected") expect(blocked.reason).toBe("origin-not-allowed");
  });

  it("accepts bare-host allowlist entries (assumed https) and ignores blank entries", () => {
    const allowOrigins = ["good.example.com", "   "];
    const allowed = checkOpenLink({ url: "https://good.example.com/x", allowOrigins });
    expect(allowed.status).toBe("ok");
    // The blank entry does not widen the list to everything.
    const blocked = checkOpenLink({ url: "https://other.example.com/x", allowOrigins });
    expect(blocked.status).toBe("rejected");
  });
});
