import { beforeEach, describe, expect, it } from "vitest";

import { _resetInputHistoryCache, inputHistory, pushInputHistory } from "./inputHistory";

const KEY = "protoagent.chat.inputHistory";

beforeEach(() => {
  localStorage.clear();
  _resetInputHistoryCache();
});

describe("inputHistory", () => {
  it("starts empty and records submitted messages oldest → newest", () => {
    expect(inputHistory()).toEqual([]);
    pushInputHistory("first");
    pushInputHistory("second");
    expect(inputHistory()).toEqual(["first", "second"]);
  });

  it("trims and ignores blank / whitespace-only input", () => {
    pushInputHistory("   ");
    pushInputHistory("");
    pushInputHistory("  keep me  ");
    expect(inputHistory()).toEqual(["keep me"]); // trimmed, blanks dropped
  });

  it("de-dupes only CONSECUTIVE repeats (like a shell)", () => {
    pushInputHistory("a");
    pushInputHistory("a"); // consecutive repeat → ignored
    pushInputHistory("b");
    pushInputHistory("a"); // not consecutive → recorded again
    expect(inputHistory()).toEqual(["a", "b", "a"]);
  });

  it("caps the ring at 100, dropping the oldest", () => {
    for (let i = 0; i < 130; i++) pushInputHistory(`m${i}`);
    const hist = inputHistory();
    expect(hist.length).toBe(100);
    expect(hist[0]).toBe("m30"); // 0–29 rotated out
    expect(hist[hist.length - 1]).toBe("m129");
  });

  it("persists across a cache reset (localStorage-backed)", () => {
    pushInputHistory("survives");
    _resetInputHistoryCache(); // simulate a fresh page load
    expect(inputHistory()).toEqual(["survives"]);
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual(["survives"]);
  });

  it("reads corrupt storage as empty instead of throwing", () => {
    localStorage.setItem(KEY, "{not json");
    _resetInputHistoryCache();
    expect(inputHistory()).toEqual([]);
  });
});
