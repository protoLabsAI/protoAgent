import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as storage from "../lib/storage";

import { _resetInputHistoryCache, inputHistory, pushInputHistory } from "./inputHistory";

const KEY = "protoagent.chat.inputHistory";

beforeEach(() => {
  localStorage.clear();
  _resetInputHistoryCache();
});

afterEach(() => vi.restoreAllMocks());

describe("inputHistory", () => {
  it("starts empty and records submitted messages oldest → newest", () => {
    expect(inputHistory("a")).toEqual([]);
    pushInputHistory("a", "first");
    pushInputHistory("a", "second");
    expect(inputHistory("a")).toEqual(["first", "second"]);
  });

  it("trims and ignores blank / whitespace-only input", () => {
    pushInputHistory("a", "   ");
    pushInputHistory("a", "");
    pushInputHistory("a", "  keep me  ");
    expect(inputHistory("a")).toEqual(["keep me"]); // trimmed, blanks dropped
  });

  it("de-dupes only CONSECUTIVE repeats (like a shell)", () => {
    pushInputHistory("a", "a");
    pushInputHistory("a", "a"); // consecutive repeat → ignored
    pushInputHistory("a", "b");
    pushInputHistory("a", "a"); // not consecutive → recorded again
    expect(inputHistory("a")).toEqual(["a", "b", "a"]);
  });

  it("caps the ring at 100, dropping the oldest", () => {
    pushInputHistory("b", "untouched");
    for (let i = 0; i < 130; i++) pushInputHistory("a", `m${i}`);
    const hist = inputHistory("a");
    expect(inputHistory("b")).toEqual(["untouched"]);
    expect(hist.length).toBe(100);
    expect(hist[0]).toBe("m30"); // 0–29 rotated out
    expect(hist[hist.length - 1]).toBe("m129");
  });

  it("persists across a cache reset (localStorage-backed)", () => {
    pushInputHistory("a", "survives");
    pushInputHistory("b", "separate");
    expect(inputHistory("a")).toEqual(["survives"]);
    expect(inputHistory("b")).toEqual(["separate"]);
    _resetInputHistoryCache(); // simulate a fresh page load
    expect(inputHistory("a")).toEqual(["survives"]);
    expect(inputHistory("b")).toEqual(["separate"]);
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual({ a: ["survives"], b: ["separate"] });
  });

  it("reads corrupt storage as empty instead of throwing", () => {
    localStorage.setItem(KEY, "{not json");
    _resetInputHistoryCache();
    expect(inputHistory("a")).toEqual([]);
  });

  it.each(['["old"]', 'null', '42', '"text"', '{"a":["valid"],"b":false}'])(
    "discards legacy or invalid storage: %s", (stored) => {
      localStorage.setItem(KEY, stored);
      expect(inputHistory("a")).toEqual([]);
      expect(inputHistory("b")).toEqual([]);
      pushInputHistory("a", "new");
      _resetInputHistoryCache();
      expect(inputHistory("a")).toEqual(["new"]);
      expect(inputHistory("b")).toEqual([]);
    },
  );

  it("filters non-string ring entries and handles prototype-like session ids", () => {
    localStorage.setItem(KEY, '{"a":["keep",1,null,false,"also keep"],"__proto__":["safe"]}');
    expect(inputHistory("a")).toEqual(["keep", "also keep"]);
    expect(inputHistory("constructor")).toEqual([]);
    pushInputHistory("__proto__", "next");
    _resetInputHistoryCache();
    expect(inputHistory("__proto__")).toEqual(["safe", "next"]);
  });

  it("evicts the least recently pushed session beyond 50, including after reload", () => {
    for (let i = 0; i < 50; i++) pushInputHistory(`session-${i}`, "message");
    // Reading does not refresh recency; even a deduplicated push does.
    inputHistory("session-1");
    pushInputHistory("session-0", "message");
    _resetInputHistoryCache();
    pushInputHistory("new-session", "new");
    _resetInputHistoryCache();
    expect(inputHistory("session-1")).toEqual([]);
    expect(inputHistory("session-0")).toEqual(["message"]);
    expect(inputHistory("session-49")).toEqual(["message"]);
    expect(inputHistory("new-session")).toEqual(["new"]);
    expect(Object.keys(JSON.parse(localStorage.getItem(KEY) as string))).toHaveLength(50);
  });

  it("keeps isolated in-memory rings when storage is blocked", () => {
    vi.spyOn(storage, "readKey").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(storage, "writeKey").mockImplementation(() => { throw new Error("blocked"); });
    expect(inputHistory("a")).toEqual([]);
    pushInputHistory("a", "alpha");
    pushInputHistory("b", "bravo");
    expect(inputHistory("a")).toEqual(["alpha"]);
    expect(inputHistory("b")).toEqual(["bravo"]);
  });

});
