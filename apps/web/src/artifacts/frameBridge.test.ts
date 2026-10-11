import { describe, expect, it } from "vitest";

import {
  checkOpenLink,
  createFrameBridge,
  createSendTrustStore,
  OPEN_LINK_FEATURES,
  PROPOSAL_RATE_LIMIT_MS,
  SEND_MAX_CHARS,
  SEND_RATE_LIMIT_MS,
  SEND_TRUST_STORAGE_KEY,
  type SendCheck,
  type SendTrustStore,
} from "./frameBridge";

// ADR 0118 D4 as amended by S19 (#4137) — "the frame proposes; the user decides." The gates are
// pure; a mutable `clock` drives the injected `now` so the rate windows are deterministic without
// fake timers, and an in-memory storage stands in for localStorage.

function memStorage(): Pick<Storage, "getItem" | "setItem"> & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return { data, getItem: (k) => data.get(k) ?? null, setItem: (k, v) => void data.set(k, v) };
}

/** A send check that passes every gate but the one under test (valid text, idle chat). */
function sendCheck(over: Partial<SendCheck> = {}): SendCheck {
  return {
    frameId: "frame-1",
    trustKey: "artifact:a-1",
    text: "Tell me more about this scenario",
    isBusy: () => false,
    ...over,
  };
}

function bridgeAt(clock: { t: number }, trust: SendTrustStore = createSendTrustStore(memStorage())) {
  return createFrameBridge({ now: () => clock.t, trust });
}

describe("send gate — the frame proposes, the user decides (#4137)", () => {
  it("an untrusted source never gets `ok` — every valid send needs the operator's confirm", () => {
    const clock = { t: 0 };
    const bridge = bridgeAt(clock);
    // Load, resume, self-focus: the host no longer reads activation/focus at all, so none of
    // those states can turn a proposal into a post. Repeated polling stays a proposal too.
    for (let i = 0; i < 9; i++) {
      clock.t = i * PROPOSAL_RATE_LIMIT_MS;
      const v = bridge.checkSend(sendCheck());
      expect(v.status).toBe("needs-confirm");
    }
    expect(bridge.checkSend(sendCheck({ trustKey: undefined })).status).not.toBe("ok");
  });

  it("needs-confirm carries the normalized text and does NOT reserve the send window", () => {
    const clock = { t: 0 };
    const bridge = bridgeAt(clock);
    const v = bridge.checkSend(sendCheck({ text: "  Run it again \n" }));
    expect(v).toEqual({ status: "needs-confirm", text: "Run it again" });
    // No send went out, so once the proposal window passes the next proposal is not rate-limited.
    clock.t = PROPOSAL_RATE_LIMIT_MS;
    expect(bridge.checkSend(sendCheck()).status).toBe("needs-confirm");
  });

  it("a trusted source goes straight through", () => {
    const bridge = bridgeAt({ t: 0 });
    bridge.trust("artifact:a-1");
    expect(bridge.checkSend(sendCheck())).toEqual({ status: "ok", text: "Tell me more about this scenario" });
  });

  it("trust is per source — another artifact still needs confirm", () => {
    const bridge = bridgeAt({ t: 0 });
    bridge.trust("artifact:a-1");
    expect(bridge.checkSend(sendCheck({ frameId: "f-2", trustKey: "artifact:a-2" })).status).toBe("needs-confirm");
  });

  it("rejects an empty or whitespace-only send (trusted or not)", () => {
    const bridge = bridgeAt({ t: 0 });
    bridge.trust("artifact:a-1");
    for (const text of ["", "   ", "\n\t "]) {
      for (const trustKey of ["artifact:a-1", "artifact:other"]) {
        const v = bridge.checkSend(sendCheck({ text, trustKey }));
        expect(v.status).toBe("rejected");
        if (v.status === "rejected") expect(v.reason).toBe("empty");
      }
    }
  });

  it("rejects a send longer than the 4000-char limit even when trusted, accepts one at the limit", () => {
    const bridge = bridgeAt({ t: 0 });
    bridge.trust("artifact:a-1");
    const tooLong = bridge.checkSend(sendCheck({ text: "x".repeat(SEND_MAX_CHARS + 1) }));
    expect(tooLong.status).toBe("rejected");
    if (tooLong.status === "rejected") expect(tooLong.reason).toBe("too-long");

    const atLimit = bridge.checkSend(sendCheck({ frameId: "frame-at-limit", text: "x".repeat(SEND_MAX_CHARS) }));
    expect(atLimit.status).toBe("ok");
  });

  it('rejects a send while the agent is busy, with the exact "the agent is busy" message — trusted too', () => {
    const bridge = bridgeAt({ t: 0 });
    bridge.trust("artifact:a-1");
    for (const trustKey of ["artifact:a-1", "artifact:untrusted"]) {
      const v = bridge.checkSend(sendCheck({ trustKey, isBusy: () => true }));
      expect(v.status).toBe("rejected");
      if (v.status === "rejected") {
        expect(v.reason).toBe("busy");
        expect(v.message).toBe("the agent is busy");
      }
    }
  });

  it("a trusted source is held to the 1-per-2s send window", () => {
    const clock = { t: 1000 };
    const bridge = bridgeAt(clock);
    bridge.trust("artifact:a-1");
    expect(bridge.checkSend(sendCheck()).status).toBe("ok"); // reserves the window at t=1000

    clock.t = 1000 + SEND_RATE_LIMIT_MS - 1;
    const tooSoon = bridge.checkSend(sendCheck());
    expect(tooSoon.status).toBe("rejected");
    if (tooSoon.status === "rejected") expect(tooSoon.reason).toBe("rate-limited");

    clock.t = 1000 + SEND_RATE_LIMIT_MS;
    expect(bridge.checkSend(sendCheck()).status).toBe("ok");
  });

  it("proposals are rate-limited per frame — a polling frame can't churn the chip", () => {
    const clock = { t: 0 };
    const bridge = bridgeAt(clock);
    expect(bridge.checkSend(sendCheck()).status).toBe("needs-confirm");
    clock.t = PROPOSAL_RATE_LIMIT_MS - 1;
    const v = bridge.checkSend(sendCheck());
    expect(v.status).toBe("rejected");
    if (v.status === "rejected") expect(v.reason).toBe("rate-limited");
    // Another frame is not blocked by this one's proposals.
    expect(bridge.checkSend(sendCheck({ frameId: "frame-2" })).status).toBe("needs-confirm");
    clock.t = PROPOSAL_RATE_LIMIT_MS;
    expect(bridge.checkSend(sendCheck()).status).toBe("needs-confirm");
  });

  it("noteSend reserves the send window once the operator confirms", () => {
    const clock = { t: 0 };
    const bridge = bridgeAt(clock);
    expect(bridge.checkSend(sendCheck()).status).toBe("needs-confirm");
    bridge.noteSend("frame-1");
    clock.t = SEND_RATE_LIMIT_MS - 1;
    const v = bridge.checkSend(sendCheck());
    expect(v.status).toBe("rejected");
    if (v.status === "rejected") expect(v.reason).toBe("rate-limited");
  });
});

describe("send trust store", () => {
  it("persists under the namespaced key and survives a fresh store (a reload)", () => {
    const storage = memStorage();
    createSendTrustStore(storage).trust("artifact:a-1");
    expect(JSON.parse(storage.data.get(SEND_TRUST_STORAGE_KEY)!)).toEqual(["artifact:a-1"]);
    const reloaded = createSendTrustStore(storage);
    expect(reloaded.isTrusted("artifact:a-1")).toBe(true);
    expect(reloaded.isTrusted("artifact:a-2")).toBe(false);
  });

  it("degrades to session-only when storage throws", () => {
    const throwing = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("QuotaExceeded");
      },
    };
    const store = createSendTrustStore(throwing);
    expect(store.isTrusted("artifact:a-1")).toBe(false);
    expect(() => store.trust("artifact:a-1")).not.toThrow();
    expect(store.isTrusted("artifact:a-1")).toBe(true);
    // And with no storage at all.
    const none = createSendTrustStore(null);
    none.trust("artifact:a-1");
    expect(none.isTrusted("artifact:a-1")).toBe(true);
  });

  it("ignores corrupt stored data rather than throwing or widening trust", () => {
    const storage = memStorage();
    storage.data.set(SEND_TRUST_STORAGE_KEY, "{not json");
    expect(createSendTrustStore(storage).isTrusted("artifact:a-1")).toBe(false);
    storage.data.set(SEND_TRUST_STORAGE_KEY, JSON.stringify({ "artifact:a-1": true }));
    expect(createSendTrustStore(storage).isTrusted("artifact:a-1")).toBe(false);
    expect(createSendTrustStore(storage).isTrusted("")).toBe(false);
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
