import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  __resetStorageSeamForTests,
  entryBytes,
  isQuotaError,
  matchKey,
  persistStorage,
  readKey,
  registerEvictionHook,
  removeKey,
  setDevHooksEnabled,
  storagePressure,
  subscribeStoragePressure,
  usageBytes,
  writeKey,
  writeKeyStrict,
} from "./storage";
import { QuotaStorage } from "./quotaStorage.testkit";

// ADR 0114 D1 — the storage seam, against a fake Storage with a byte quota.

let local: QuotaStorage;
let session: QuotaStorage;

beforeEach(() => {
  __resetStorageSeamForTests();
  local = new QuotaStorage(1000);
  session = new QuotaStorage(1000);
  vi.stubGlobal("localStorage", local);
  vi.stubGlobal("sessionStorage", session);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetStorageSeamForTests();
  delete (globalThis as { __protoagentSimulateQuotaBytes?: number }).__protoagentSimulateQuotaBytes;
});

const big = (n: number) => "x".repeat(n);

describe("isQuotaError", () => {
  it("recognises every engine's shape, by name or code", () => {
    expect(isQuotaError(new DOMException("x", "QuotaExceededError"))).toBe(true);
    expect(isQuotaError({ name: "NS_ERROR_DOM_QUOTA_REACHED" })).toBe(true);
    expect(isQuotaError({ name: "Error", code: 22 })).toBe(true);
    expect(isQuotaError({ name: "Error", code: 1014 })).toBe(true);
    expect(isQuotaError(new Error("nope"))).toBe(false);
    expect(isQuotaError(new DOMException("x", "SecurityError"))).toBe(false);
    expect(isQuotaError(null)).toBe(false);
  });
});

describe("readKey / writeKey never throw", () => {
  it("round-trips and reads a missing key as null", () => {
    expect(writeKey("local", "protoagent.editor", "zed")).toEqual({ ok: true });
    expect(readKey("local", "protoagent.editor")).toBe("zed");
    expect(readKey("local", "nope")).toBeNull();
  });

  it("reports `unavailable` when the storage accessor itself throws (disabled storage)", () => {
    vi.stubGlobal("localStorage", undefined);
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new DOMException("denied", "SecurityError");
      },
    });
    expect(readKey("local", "k")).toBeNull();
    expect(writeKey("local", "k", "v")).toEqual({ ok: false, reason: "unavailable" });
    expect(() => removeKey("local", "k")).not.toThrow();
  });

  it("reports `unavailable` for a non-quota setItem error", () => {
    vi.spyOn(local, "setItem").mockImplementation(() => {
      throw new TypeError("weird");
    });
    expect(writeKey("local", "protoagent.editor", "zed")).toEqual({ ok: false, reason: "unavailable" });
    expect(storagePressure().state).toBe("ok");
  });
});

describe("the quota latch: evict → retry → latch", () => {
  it("runs the eviction hook once, retries once, and succeeds when it freed space", () => {
    local.setItem("protoagent.chat.sessions:old", big(400)); // ~800 bytes of a stale transcript
    const hook = vi.fn(() => {
      const v = local.getItem("protoagent.chat.sessions:old") ?? "";
      local.removeItem("protoagent.chat.sessions:old");
      return entryBytes("protoagent.chat.sessions:old", v);
    });
    registerEvictionHook(hook);
    expect(writeKey("local", "protoagent.chat.sessions", big(200))).toEqual({ ok: true });
    expect(hook).toHaveBeenCalledTimes(1);
    expect(storagePressure().state).toBe("ok");
  });

  it("latches `failing` when the retry still fails, then fails evictable writes FAST", () => {
    local.setItem("plugin.hog", big(450)); // unregistered — never touched
    const hook = vi.fn(() => 0);
    registerEvictionHook(hook);
    const seen: string[] = [];
    subscribeStoragePressure(() => seen.push(storagePressure().state));

    expect(writeKey("local", "protoagent.chat.sessions", big(100))).toEqual({ ok: false, reason: "quota" });
    expect(hook).toHaveBeenCalledTimes(1);
    expect(local.setCalls).toBe(3); // the seed + the attempt + the one retry
    expect(storagePressure().state).toBe("failing");
    expect(seen).toEqual(["failing"]);

    // Latched: transcript + layout writes return quota without touching storage or evicting.
    expect(writeKey("local", "protoagent.chat.sessions", big(100))).toEqual({ ok: false, reason: "quota" });
    expect(writeKey("local", "protoagent.ui:agentx", "{}")).toEqual({ ok: false, reason: "quota" });
    expect(local.setCalls).toBe(3);
    expect(hook).toHaveBeenCalledTimes(1);

    // Non-evictable categories (credentials, prefs) still try — a small one fits.
    expect(writeKey("local", "protoagent.authToken", "tok")).toEqual({ ok: true });
    expect(readKey("local", "protoagent.authToken")).toBe("tok");
  });

  it("releases the latch on a removal", () => {
    local.setItem("plugin.hog", big(480));
    expect(writeKey("local", "protoagent.chat.sessions", big(100)).ok).toBe(false);
    expect(storagePressure().state).toBe("failing");
    removeKey("local", "plugin.hog");
    expect(storagePressure().state).toBe("ok");
    expect(writeKey("local", "protoagent.chat.sessions", big(100))).toEqual({ ok: true });
  });

  it("releases the latch on a cross-tab `storage` event", () => {
    local.setItem("plugin.hog", big(480));
    writeKey("local", "protoagent.chat.sessions", big(100));
    expect(storagePressure().state).toBe("failing");
    window.dispatchEvent(new StorageEvent("storage", { key: "plugin.hog", newValue: null }));
    expect(storagePressure().state).toBe("ok");
  });

  it("releases the latch when a later eviction frees space", () => {
    local.setItem("plugin.hog", big(480));
    writeKey("local", "protoagent.chat.sessions", big(100));
    expect(storagePressure().state).toBe("failing");
    // A non-evictable write still reaches the hook; this time it frees something.
    registerEvictionHook(() => {
      local.removeItem("plugin.hog");
      return 960;
    });
    expect(writeKey("local", "protoagent.keybindings", big(300))).toEqual({ ok: true });
    expect(storagePressure().state).toBe("ok");
  });

  it("sessionStorage quota never evicts and never latches", () => {
    const hook = vi.fn(() => 0);
    registerEvictionHook(hook);
    expect(writeKey("session", "protoagent.events.since", big(600))).toEqual({ ok: false, reason: "quota" });
    expect(hook).not.toHaveBeenCalled();
    expect(storagePressure().state).toBe("ok");
  });

  it("treats Firefox's NS_ERROR_DOM_QUOTA_REACHED (code 1014) the same", () => {
    local = new QuotaStorage(100, () => Object.assign(new Error("full"), { name: "NS_ERROR_DOM_QUOTA_REACHED", code: 1014 }));
    vi.stubGlobal("localStorage", local);
    expect(writeKey("local", "protoagent.chat.sessions", big(100))).toEqual({ ok: false, reason: "quota" });
    expect(storagePressure().state).toBe("failing");
  });
});

describe("the latch never blocks a write that frees space (review r1)", () => {
  // latch.mts: a transcript grows past the quota → latched. The operator then DELETES chats,
  // so the next write of that same key is smaller. Failing it fast would bring the deleted
  // chats back on reload — and with no eviction yet (S1) the latch would never release.
  it("a shrinking write to an evictable key goes through and releases the latch", () => {
    local = new QuotaStorage(10_000);
    vi.stubGlobal("localStorage", local);
    const K = "protoagent.chat.sessions";
    expect(writeKey("local", K, big(4000))).toEqual({ ok: true }); // ~8 KB
    expect(writeKey("local", K, big(6000))).toEqual({ ok: false, reason: "quota" });
    expect(storagePressure().state).toBe("failing");
    expect(writeKey("local", K, big(100))).toEqual({ ok: true });
    expect(readKey("local", K)?.length).toBe(100);
    expect(storagePressure().state).toBe("ok");
    expect(writeKey("local", "protoagent.ui", "{}")).toEqual({ ok: true }); // layout saves again
  });

  it("an equal-size rewrite also goes through (usage doesn't grow)", () => {
    local.setItem("protoagent.ui", big(100));
    local.setItem("plugin.hog", big(370)); // 988 of 1000 bytes used
    expect(writeKey("local", "protoagent.chat.sessions", big(50)).ok).toBe(false);
    expect(storagePressure().state).toBe("failing");
    expect(writeKey("local", "protoagent.ui", "y".repeat(100))).toEqual({ ok: true });
    expect(storagePressure().state).toBe("ok");
  });

  it("a GROWING evictable write still fails fast while latched", () => {
    local.setItem("protoagent.ui", big(10));
    local.setItem("plugin.hog", big(450));
    expect(writeKey("local", "protoagent.chat.sessions", big(100)).ok).toBe(false);
    const calls = local.setCalls;
    expect(writeKey("local", "protoagent.ui", big(20))).toEqual({ ok: false, reason: "quota" });
    expect(local.setCalls).toBe(calls);
  });
});

describe("eviction isn't re-run for a write that already failed at that size (review r1)", () => {
  it("records the failed size per key while latched", () => {
    local.setItem("plugin.hog", big(450));
    const hook = vi.fn(() => 0);
    registerEvictionHook(hook);
    expect(writeKey("local", "protoagent.keybindings", big(100)).ok).toBe(false); // evicts once
    expect(hook).toHaveBeenCalledTimes(1);
    expect(writeKey("local", "protoagent.keybindings", big(100)).ok).toBe(false); // same size
    expect(writeKey("local", "protoagent.keybindings", big(120)).ok).toBe(false); // larger
    expect(hook).toHaveBeenCalledTimes(1);
    expect(writeKey("local", "protoagent.inputHistoryish", big(100)).ok).toBe(false); // another key
    expect(hook).toHaveBeenCalledTimes(2);
    // A release (here: a removal) clears the memo — the next failure may evict again.
    removeKey("local", "protoagent.editor");
    expect(writeKey("local", "protoagent.keybindings", big(100)).ok).toBe(false);
    expect(hook).toHaveBeenCalledTimes(3);
  });
});

describe("writeKeyStrict", () => {
  it("throws when the browser can't keep the value — never reports a silent success", () => {
    local.setItem("plugin.hog", big(490));
    expect(() => writeKeyStrict("local", "protoagent.authToken", big(50))).toThrow(/storage is full/);
    expect(readKey("local", "protoagent.authToken")).toBeNull();
  });
});

describe("storage.simulateQuotaBytes (dev flag)", () => {
  afterEach(() => setDevHooksEnabled(true)); // vitest runs as a dev build

  it("is ignored in production (dev hooks off)", () => {
    setDevHooksEnabled(false);
    (globalThis as { __protoagentSimulateQuotaBytes?: number }).__protoagentSimulateQuotaBytes = 10;
    expect(writeKey("local", "protoagent.editor", "zed")).toEqual({ ok: true });
  });

  it("throws a synthetic QuotaExceededError once total usage would exceed N bytes", () => {
    local = new QuotaStorage(10_000_000);
    vi.stubGlobal("localStorage", local);
    (globalThis as { __protoagentSimulateQuotaBytes?: number }).__protoagentSimulateQuotaBytes = 200;
    expect(writeKey("local", "protoagent.editor", "zed")).toEqual({ ok: true }); // 2*(17+3)=40
    expect(usageBytes("local")).toBe(40);
    expect(writeKey("local", "protoagent.chat.sessions", big(100))).toEqual({ ok: false, reason: "quota" });
    expect(storagePressure().state).toBe("failing");
    // Overwriting an existing key counts the delta, not the sum.
    expect(writeKey("local", "protoagent.editor", "vscode")).toEqual({ ok: true });
  });
});

describe("key registry — exact patterns, never prefixes", () => {
  const cat = (k: string, area: "local" | "session" = "local") => matchKey(area, k)?.spec.category ?? null;
  const slug = (k: string, area: "local" | "session" = "local") => matchKey(area, k)?.slug;

  it("chat transcripts, with the .dismissed set NOT mistaken for one", () => {
    expect(cat("protoagent.chat.sessions")).toBe("transcript");
    expect(slug("protoagent.chat.sessions")).toBe("host");
    expect(cat("protoagent.chat.sessions:navaEngineer-07cb")).toBe("transcript");
    expect(slug("protoagent.chat.sessions:navaEngineer-07cb")).toBe("navaEngineer-07cb");
    expect(cat("protoagent.chat.sessions.dismissed")).toBe("dismissals");
    expect(cat("protoagent.chat.sessions:gymBro.dismissed")).toBe("dismissals");
    expect(slug("protoagent.chat.sessions:gymBro.dismissed")).toBe("gymBro");
    expect(matchKey("local", "protoagent.chat.sessions:gymBro.dismissed")?.spec.evictable).toBe(false);
    // Near-misses are unregistered (counted, never touched), not swept up by a prefix.
    expect(cat("protoagent.chat.sessionsX")).toBeNull();
    expect(cat("protoagent.chat.sessions:a:b")).toBeNull();
  });

  it("palette and DM threads, host and per agent", () => {
    expect(cat("protoagent.palette.chat")).toBe("transcript");
    expect(slug("protoagent.palette.chat")).toBe("host");
    expect(slug("protoagent.palette.chat:mothership")).toBe("mothership");
    expect(slug("protoagent.palette.chat:mothership:dm:gymBro")).toBe("mothership");
    expect(cat("protoagent.palette.chat:dm:gymBro")).toBe("transcript");
    expect(slug("protoagent.palette.chat:dm:gymBro")).toBe("host");
    expect(cat("protoagent.palette.recent")).toBe("prefs");
  });

  it("layout, credentials, theme and per-tab keys", () => {
    expect(cat("protoagent.ui")).toBe("layout");
    expect(slug("protoagent.ui:x")).toBe("x");
    expect(cat("proto:uislice:dev-flags")).toBe("layout");
    expect(slug("proto:uislice:dev-flags:x")).toBe("x");
    expect(cat("protoagent.authToken")).toBe("auth");
    expect(matchKey("local", "protoagent.authToken")?.spec.evictable).toBe(false);
    expect(cat("protoagent.tenant.uid")).toBe("tenant");
    expect(cat("pl-theme")).toBe("theme");
    expect(cat("protoagent.keybindings")).toBe("prefs");
    expect(cat("protoagent.chat.draft:host:s1", "session")).toBe("ephemeral");
    expect(cat("protoagent.events.since:x", "session")).toBe("ephemeral");
    // Area matters: a session key isn't a local one.
    expect(cat("protoagent.events.since", "local")).toBeNull();
  });

  it("every evictable category is transcript or layout", () => {
    for (const k of ["protoagent.chat.sessions", "protoagent.palette.chat", "protoagent.ui", "proto:uislice:a"]) {
      expect(matchKey("local", k)?.spec.evictable).toBe(true);
    }
  });
});

describe("persistStorage (the zustand adapter)", () => {
  it("getItem returns exactly string | null and setItem never throws", () => {
    const s = persistStorage("local", (n) => `${n}:agentx`);
    expect(s.getItem("protoagent.ui")).toBeNull();
    s.setItem("protoagent.ui", "{}");
    expect(s.getItem("protoagent.ui")).toBe("{}");
    expect(local.getItem("protoagent.ui:agentx")).toBe("{}");
    local.setItem("plugin.hog", big(450));
    expect(() => s.setItem("protoagent.ui", big(400))).not.toThrow();
    s.removeItem("protoagent.ui");
    expect(local.getItem("protoagent.ui:agentx")).toBeNull();
  });

  it("skips a write whose value storage already holds", () => {
    const s = persistStorage("local");
    s.setItem("protoagent.keybindings", '{"a":1}');
    const before = local.setCalls;
    s.setItem("protoagent.keybindings", '{"a":1}');
    expect(local.setCalls).toBe(before);
    s.setItem("protoagent.keybindings", '{"a":2}');
    expect(local.setCalls).toBe(before + 1);
  });
});
