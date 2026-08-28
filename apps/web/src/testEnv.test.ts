import { beforeEach, describe, expect, it } from "vitest";

// Environment guard (#3213). Storage-backed suites — the chat input-history ring, the uiStore
// persist migration, scratch state — assume working `localStorage`/`sessionStorage`. When the
// host Node pre-defines those globals (Node 25 promoted Web Storage to enabled-by-default),
// vitest's jsdom environment leaves the pre-existing accessor alone and jsdom's Storage never
// lands: `localStorage` throws on 25 / reads `undefined` on 26, and 133 resp. 127 tests die on
// `localStorage.clear()` with no hint that the Node version is the cause. vitest.setup.ts repairs that; this file is what fails
// FIRST, and legibly, if the repair ever stops working.
//
// The repair installs Storage from a second JSDOM realm, so it must bring the `Storage`
// constructor across too — otherwise `vi.spyOn(Storage.prototype, "setItem")`, which the
// chat-store persist tests use to count writes, patches a prototype nothing is an instance of.
// That coupling is asserted here rather than left to be rediscovered.

describe("test environment: Web Storage", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it("exposes both storages as Storage objects from one implementation", () => {
    expect(Object.prototype.toString.call(localStorage)).toBe("[object Storage]");
    expect(Object.prototype.toString.call(sessionStorage)).toBe("[object Storage]");
    // Same prototype ⇒ same realm: catches a half-repaired pair (one jsdom, one Node's).
    expect(Object.getPrototypeOf(localStorage)).toBe(Object.getPrototypeOf(sessionStorage));
  });

  it("keeps the global Storage constructor in the same realm as the instances", () => {
    // What makes `vi.spyOn(Storage.prototype, "setItem")` observe real writes (chat-store's
    // persist-debounce tests). A mismatch here is invisible: the spy installs fine and simply
    // never fires.
    expect(localStorage).toBeInstanceOf(Storage);
    expect(Object.getPrototypeOf(localStorage)).toBe(Storage.prototype);
    expect(window.Storage).toBe(Storage);
  });

  it("round-trips values with Storage semantics", () => {
    expect(localStorage.getItem("absent")).toBeNull(); // missing key is null, not undefined
    localStorage.setItem("k", "v");
    expect(localStorage.getItem("k")).toBe("v");
    expect(localStorage.length).toBe(1);
    localStorage.removeItem("k");
    expect(localStorage.getItem("k")).toBeNull();
  });

  it("coerces non-string values the way the platform does", () => {
    // The persist middleware writes strings; the coercion is what makes a stray number read back
    // as "1" rather than 1 — worth pinning, since a hand-rolled stand-in could get it wrong.
    localStorage.setItem("n", 1 as unknown as string);
    expect(localStorage.getItem("n")).toBe("1");
  });

  it("keeps the two storages separate", () => {
    localStorage.setItem("shared-key", "local");
    sessionStorage.setItem("shared-key", "session");
    expect(localStorage.getItem("shared-key")).toBe("local");
    expect(sessionStorage.getItem("shared-key")).toBe("session");
  });

  it("starts each test file with empty storage", () => {
    // Per-file isolation keeps one suite's persisted state out of the next one's assertions.
    // (Verified as intact on Node 25/26 before the shim — vitest gives each file a fresh
    // environment — so the shim must not be what breaks it.)
    localStorage.clear();
    expect(localStorage.length).toBe(0);
  });
});
